"""Tests for plan step 7, the Yahoo v8 chart fetch and parse (scripts/reversal_data_yahoo.py). No network."""
import gzip
import io
import json
from datetime import datetime, timezone
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_yahoo as yh


def _stamp(day: str, hour: int = 13, minute: int = 30) -> int:
    """Epoch of ``day`` at hh:mm UTC (13:30 UTC is the 09:30 New York open in summer)."""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute, tzinfo=timezone.utc).timestamp())


def _payload(days, closes, volumes, splits=(), dividends=(), meta=None):
    result = {
        "meta": {"symbol": "TEST", "longName": "Test Corp", "shortName": "Test Corp", "instrumentType": "EQUITY",
                 "currency": "USD", "exchangeName": "NMS", "firstTradeDate": _stamp("2005-03-01"), **(meta or {})},
        "timestamp": [_stamp(d) for d in days],
        "events": {
            "splits": {str(_stamp(d)): {"date": _stamp(d), "numerator": n, "denominator": m, "splitRatio": f"{n}:{m}"}
                       for d, n, m in splits},
            "dividends": {str(_stamp(d)): {"date": _stamp(d), "amount": a} for d, a in dividends},
        },
        "indicators": {"quote": [{"close": list(closes), "volume": list(volumes), "open": list(closes),
                                  "high": list(closes), "low": list(closes)}],
                       "adjclose": [{"adjclose": list(closes)}]},
    }
    return {"chart": {"result": [result], "error": None}}


# ------------------------------------------------------------------ requests and URL

def test_chart_url_asks_for_daily_bars_with_dividends_splits_and_adjusted_close():
    url = yh.chart_url("AAPL", "2011-06-01", "2026-10-02")
    assert url.startswith("https://query1.finance.yahoo.com/v8/finance/chart/AAPL?")
    assert "period1=1306886400" in url and "period2=1790899200" in url
    assert "interval=1d" in url and "events=div,splits" in url and "includeAdjustedClose=true" in url


def test_request_rows_take_yahoo_rows_and_the_v_sample_once_per_security():
    candidates = pd.DataFrame([
        {"security_id": "1", "ticker_for_source": "AAA", "needed_start": "2018-01-21", "needed_end": "2026-08-31",
         "reason": "A1_wiki_dv_rank300", "planned_source": "yahoo", "active": "Y", "note": ""},
        {"security_id": "1", "ticker_for_source": "AAA", "needed_start": "2011-06-01", "needed_end": "2026-08-31",
         "reason": "V_verify_sample", "planned_source": "tiingo", "active": "Y", "note": "verification"},
        {"security_id": "2", "ticker_for_source": "BBB", "needed_start": "2018-01-21", "needed_end": "2020-01-01",
         "reason": "B_A_float_ge_1B", "planned_source": "tiingo", "active": "N", "note": ""},
        {"security_id": "3", "ticker_for_source": "CCC", "needed_start": "2020-01-01", "needed_end": "2022-05-10",
         "reason": "Y_active_rank300", "planned_source": "yahoo", "active": "successor", "note": "history under 4"},
    ])
    rows = yh.request_rows(candidates).set_index("security_id")
    assert list(rows.index) == ["1", "3"]
    assert rows.loc["1", "needed_start"] == "2011-06-01" and rows.loc["1", "reasons"] == "A1_wiki_dv_rank300 V_verify_sample"
    assert rows.loc["3", "successor_routed"] == "Y" and rows.loc["1", "successor_routed"] == ""


# ------------------------------------------------------------------ parsing

def test_parse_restores_raw_close_volume_and_dividends_from_split_adjusted_values():
    # Raw: 100, 102 then a 2:1 split (51), a $1.00 raw dividend before the split, then a 1:4 reverse split.
    days = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08"]
    # Yahoo values are adjusted for both splits: factor before 01-06 is 2 * 0.25 = 0.5, then 0.25, then 1.
    closes = [100 / 0.5, 102 / 0.5, 51 / 0.25, 50 / 0.25, 200.0]
    volumes = [1000 * 0.5, 1000 * 0.5, 2000 * 0.25, 2000 * 0.25, 500]
    payload = _payload(days, closes, volumes, splits=[("2020-01-06", 2, 1), ("2020-01-08", 1, 4)],
                       dividends=[("2020-01-03", 1.0 / 0.5)])
    meta, daily, events = yh.parse_chart(payload)
    assert list(daily["date"].dt.strftime("%Y-%m-%d")) == days
    assert np.allclose(daily["close_raw"], [100, 102, 51, 50, 200])
    assert np.allclose(daily["volume_raw"], [1000, 1000, 2000, 2000, 500])
    assert np.allclose(daily["split_factor"], [1, 1, 2, 1, 0.25])
    assert np.allclose(daily["div_cash"], [0, 1.0, 0, 0, 0])
    assert np.allclose(daily["split_cum_after"], [0.5, 0.5, 0.25, 0.25, 1])
    splits = events[events["event_type"] != "dividend"]
    assert list(splits["event_type"]) == ["split", "reverse_split"]
    dividend = events[events["event_type"] == "dividend"].iloc[0]
    assert dividend["div_cash_raw"] == pytest.approx(1.0) and dividend["amount_yahoo"] == pytest.approx(2.0)
    assert meta["symbol"] == "TEST"


def test_parse_uses_new_york_dates_drops_null_rows_and_a_repeated_last_day():
    days = ["2021-03-01", "2021-03-02", "2021-03-03"]
    payload = _payload(days, [10.0, None, 11.0], [100, 0, 120])
    # The live last row repeats 03-03 with a later stamp (20:00 UTC = 16:00 New York).
    result = payload["chart"]["result"][0]
    result["timestamp"].append(_stamp("2021-03-03", 20, 0))
    result["indicators"]["quote"][0]["close"].append(11.5)
    result["indicators"]["quote"][0]["volume"].append(130)
    result["indicators"]["adjclose"][0]["adjclose"].append(11.5)
    _, daily, _ = yh.parse_chart(payload)
    assert list(daily["date"].dt.strftime("%Y-%m-%d")) == ["2021-03-01", "2021-03-03"]
    assert daily["close_raw"].iloc[-1] == pytest.approx(11.5)
    # A stamp at 03:00 UTC is still the previous New York day.
    assert yh._ny_dates([_stamp("2021-03-02", 3, 0)])[0] == pd.Timestamp("2021-03-01")


def test_parse_of_an_error_body_is_empty():
    meta, daily, events = yh.parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}})
    assert daily.empty and events.empty and "Not Found" in meta["error"]


def test_event_flags_mark_odd_ratios_late_splits_and_special_dividends():
    days = ["2019-02-06", "2019-02-07", "2019-02-08", "2024-05-01", "2024-05-02"]
    payload = _payload(days, [80 / 1.275, 79.69 / 1.275, 59.15, 30.0, 31.0], [1, 1, 1, 1, 1],
                       splits=[("2019-02-08", 1275, 1000)], dividends=[("2024-05-02", 4.0)])
    _, daily, events = yh.parse_chart(payload)
    flagged = yh.event_flags(events, daily).set_index("event_type")
    # No WIKI volume to tell whether Yahoo scaled volume for this distribution: restored, flagged.
    assert flagged.loc["split", "flags"] == "odd_ratio volume_restore_unverified"
    assert flagged.loc["split", "volume_restored"] == "Y" and flagged.loc["split", "volume_evidence"] == "none"
    assert flagged.loc["split", "prior_close_raw"] == pytest.approx(79.69)
    assert flagged.loc["dividend", "flags"] == "special_dividend_gt10pct"
    assert flagged.loc["dividend", "pct_of_prior"] == pytest.approx(4 / 30)
    assert yh.ordinary_ratio(7.0) and yh.ordinary_ratio(0.25) and yh.ordinary_ratio(1.5)
    assert yh.ordinary_ratio(1 / 50) and yh.ordinary_ratio(25.0)
    assert not yh.ordinary_ratio(1.061) and not yh.ordinary_ratio(1.275) and not yh.ordinary_ratio(1.05)


def test_a_listed_split_that_yahoo_did_not_apply_is_left_out_of_the_restore():
    # A 1:30 reverse split two weeks before the fetch: the served closes are still unadjusted.
    days = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
    payload = _payload(days, [0.16, 0.11, 2.73, 2.05], [100, 100, 10, 10], splits=[("2026-09-14", 1, 30)])
    _, daily, events = yh.parse_chart(payload)
    assert np.allclose(daily["close_raw"], [0.16, 0.11, 2.73, 2.05])
    assert np.allclose(daily["split_factor"], [1, 1, 1 / 30, 1])
    flagged = yh.event_flags(events, daily)
    assert flagged.loc[0, "applied_by_yahoo"] == "N" and "not_applied_by_yahoo" in flagged.loc[0, "flags"]
    assert yh.split_restore_flags(daily, events) == []


def test_split_day_flag_catches_a_move_that_is_neither_adjusted_nor_raw():
    days = ["2025-05-05", "2025-05-06", "2025-05-07", "2025-05-08"]
    good = _payload(days, [100.0, 101.0, 102.0, 103.0], [1, 1, 1, 1], splits=[("2025-05-07", 1, 20)])
    _, daily, events = yh.parse_chart(good)
    assert yh.split_restore_flags(daily, events) == [] and events.loc[0, "applied_by_yahoo"] == "Y"
    odd = _payload(days, [900.0, 900.0, 75.0, 90.0], [1, 1, 1, 1], splits=[("2025-05-07", 1, 20)])
    _, daily, events = yh.parse_chart(odd)
    assert yh.split_restore_flags(daily, events) == ["split_day_move:2025-05-07:1:20"]


# ------------------------------------------------------------------ names and levels

@pytest.mark.parametrize("left,right,same", [
    ("HENRY SCHEIN INC", "Henry Schein, Inc.", True),
    ("UNIVERSAL DISPLAY CORP \\PA\\", "Universal Display Corporation", True),
    ("SOHU COM INC", "Sohu.com Limited", True),
    ("QUIDEL CORP /DE/", "QuidelOrtho Corporation", True),
    ("Liberty Global Ltd. - Class C Ordinary Shares", "Liberty Global Ltd.", True),
    ("American Airlines Group Inc", "American Express Company", False),
    ("First Solar, Inc.", "First Horizon Corporation", False),
    ("PAYPAL HOLDINGS INC", "eBay Inc.", False),
    ("Strategy Inc", "MicroStrategy Incorporated", False),
])
def test_names_match(left, right, same):
    assert yh.names_match(left, right) is same


def test_former_names_are_split_from_their_date_ranges():
    text = "Discovery, Inc. (2018-03-06..2022-04-08) | Discovery Communications, Inc. (2008-06-11..2018-03-05)"
    assert yh.former_name_list(text) == ["Discovery, Inc.", "Discovery Communications, Inc."]
    assert yh.former_name_list("") == []


def _daily(days, closes, volumes=None):
    return pd.DataFrame({"date": pd.to_datetime(days), "close_raw": closes,
                         "volume_raw": volumes if volumes is not None else [1000] * len(days)})


def test_lastsale_wiki_and_stored_checks():
    daily = _daily(["2015-01-02", "2015-06-01", "2016-01-04"], [10.0, 20.0, 30.0])
    quotes = pd.DataFrame({"as_of_session": ["2015-01-02", "2015-06-01", "2016-01-04"],
                           "last_sale": [10.1, 20.0, 45.0], "as_of_check": ["confirmed", "unverified", "unverified"]})
    result = yh.lastsale_check(daily, quotes)
    assert (result["lastsale_n"], result["lastsale_agree"], result["lastsale_fail"]) == (3, 2, False)
    quotes["last_sale"] = [15.0, 30.0, 45.0]
    assert yh.lastsale_check(daily, quotes)["lastsale_fail"]
    many = _daily(pd.bdate_range("2015-01-01", periods=30), [10.0] * 30)
    wiki = pd.DataFrame({"date": many["date"], "close": [10.05] * 10 + [12.0] * 20})
    assert yh.wiki_check(many, wiki)["wiki_fail"]
    wiki["close"] = 10.0
    assert not yh.wiki_check(many, wiki)["wiki_fail"] and yh.wiki_check(many, wiki)["wiki_share_1pct"] == 1.0
    long_ = _daily(pd.bdate_range("2019-01-01", periods=80), [20.0] * 80, [1000] * 80)
    stored = pd.DataFrame({"date": long_["date"], "close": [10.0] * 80, "volume": [2000] * 80})  # same dollar volume
    assert not yh.stored_dv_check(long_, stored)["stored_dv_off"]
    stored["volume"] = 400
    result = yh.stored_dv_check(long_, stored)
    assert result["stored_dv_off"] and result["stored_dv_median"] == pytest.approx(5.0)


class _Refs(yh.References):
    """References built from small in-memory frames, with no WIKI or stored files on disk."""

    def __init__(self, master, quotes=None, wiki=None, stored=None):
        intervals = pd.DataFrame(columns=["security_id", "name_in_source"])
        lists = quotes if quotes is not None else pd.DataFrame(
            columns=["security_id", "as_of_session", "last_sale", "as_of_check"])
        super().__init__(pd.DataFrame(master), intervals, lists, pd.DataFrame(columns=["security_id", "src"]))
        self._wiki, self._stored = wiki, stored

    def wiki(self, sid):
        return self._wiki if self._wiki is not None else pd.DataFrame(columns=["date", "close", "volume"])

    def stored(self, sid):
        return self._stored if self._stored is not None else pd.DataFrame(columns=["date", "close", "volume"])


MASTER_ROW = {"security_id": "9", "name": "Test Corp", "former_names": "", "first_listed": "2010-12-31",
              "delist_date": "", "successor_security_id": ""}


def _check(meta=None, days=None, closes=None, request=None, refs=None):
    days = days if days is not None else pd.bdate_range("2018-01-22", "2026-08-31")
    daily = _daily(days, closes if closes is not None else [50.0] * len(days))
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    request = {"security_id": "9", "symbol": "TEST", "needed_start": "2018-01-22", "needed_end": "2026-08-31",
               "successor_routed": "", **(request or {})}
    meta = {"symbol": "TEST", "longName": "Test Corp", "instrumentType": "EQUITY", "currency": "USD",
            "firstTradeDate": _stamp("2005-03-01"), **(meta or {})}
    return yh.check_entity(request, meta, daily, refs or _Refs([MASTER_ROW]), sessions, "2026-10-01")


def test_entity_check_ok_partial_and_wrong_entity():
    assert _check()["verdict"] == "ok"
    late = _check(days=pd.bdate_range("2019-06-03", "2026-08-31"))
    assert late["verdict"] == "partial" and late["verdict_reasons"].startswith("starts 2019-06-03")
    # A newer company on the ticker: another name and a first trade after the security was listed.
    newer = _check(meta={"longName": "Other Robotics Inc", "firstTradeDate": _stamp("2021-05-03")})
    assert newer["verdict"] == "wrong_entity" and "name_and_first_trade" in newer["verdict_reasons"]
    # Same name, but the price level disagrees with the company lists.
    quotes = pd.DataFrame({"security_id": ["9", "9"], "as_of_session": ["2018-02-01", "2019-03-01"],
                           "last_sale": ["80", "81"], "as_of_check": ["confirmed", "confirmed"]})
    level = _check(refs=_Refs([MASTER_ROW], quotes=quotes))
    assert level["verdict"] == "wrong_entity" and level["verdict_reasons"].startswith("lastsale_level:0/2")
    fund = _check(meta={"instrumentType": "ETF"})
    assert fund["verdict"] == "wrong_entity"
    # A name mismatch alone (no level evidence) is left for review.
    renamed = _check(meta={"longName": "Brand New Name Inc"})
    assert renamed["verdict"] == "review" and "name_mismatch_no_level_check" in renamed["verdict_reasons"]


def test_entity_check_uses_the_successor_names_for_successor_routed_rows():
    master = [{**MASTER_ROW, "name": "Old Holdings OpCo", "successor_security_id": "10"},
              {**MASTER_ROW, "security_id": "10", "name": "Zeta Gaming Inc", "first_listed": "2022-05-11"}]
    request = {"successor_routed": "Y", "needed_end": "2022-05-10"}
    days = pd.bdate_range("2018-01-22", "2022-05-10")
    result = _check(meta={"longName": "Zeta Gaming Inc."}, days=days, request=request, refs=_Refs(master))
    assert result["name_match"] and result["verdict"] == "ok"


# ------------------------------------------------------------------ fetch loop

class _FakeGetter:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def __call__(self, url, path, **kwargs):
        symbol = kwargs["symbol"]
        self.calls.append(symbol)
        action = self.behaviour.get(symbol, "ok")
        if action == "404":
            path.with_name(path.name + ".404").write_bytes(b"")
            raise FileNotFoundError("404")
        if action == "429":
            raise HTTPError(url, 429, "Too Many Requests", {}, None)
        if action == "error":
            raise RuntimeError("timed out")
        data = json.dumps(_payload(["2020-01-02"], [1.0], [1])).encode()
        path.write_bytes(gzip.compress(data))
        return data


def test_fetch_stops_at_the_first_429_and_resumes_without_refetching(tmp_path):
    getter = _FakeGetter({"BBB": "404", "CCC": "error", "DDD": "429"})
    outcome = yh.fetch_symbols(["AAA", "BBB", "CCC", "DDD", "EEE"], period2="2026-10-02", raw_dir=tmp_path,
                               getter=getter)
    assert getter.calls == ["AAA", "BBB", "CCC", "DDD"]  # EEE is never asked after the 429
    assert {s: o["status"] for s, o in outcome.items()} == {"AAA": "ok", "BBB": "not_found", "CCC": "error",
                                                             "DDD": "stopped"}
    assert yh.cached_raw("AAA", tmp_path)[1] == "ok" and yh.cached_raw("BBB", tmp_path)[1] == "not_found"
    again = _FakeGetter({})
    yh.fetch_symbols(["AAA", "BBB", "CCC", "DDD", "EEE"], period2="2026-10-02", raw_dir=tmp_path, getter=again)
    assert again.calls == ["CCC", "DDD", "EEE"]


def test_cached_raw_does_not_match_a_longer_symbol(tmp_path):
    (tmp_path / "ABCD__20261001T000000Z.json.gz").write_bytes(b"")
    assert yh.cached_raw("ABC", tmp_path) == (None, "")


# ------------------------------------------------------------------ build

def test_build_writes_per_security_files_events_and_reports(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    days = list(pd.bdate_range("2018-01-22", "2026-08-31").strftime("%Y-%m-%d"))
    payload = _payload(days, [25.0] * len(days), [1000] * len(days), splits=[(days[100], 2, 1)],
                       dividends=[(days[200], 0.5)])
    (raw / "TEST__20261001T010203Z.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
    (raw / "GONE__20261001T010205Z.json.gz.404").write_bytes(b"")
    requests = pd.DataFrame([
        {"security_id": "9", "symbol": "TEST", "needed_start": "2018-01-22", "needed_end": "2026-08-31",
         "reasons": "Y_active_rank300", "active": "Y", "successor_routed": "", "note": ""},
        {"security_id": "8", "symbol": "GONE", "needed_start": "2018-01-22", "needed_end": "2026-08-31",
         "reasons": "A1_wiki_dv_rank300", "active": "Y", "successor_routed": "", "note": ""},
        {"security_id": "7", "symbol": "TEST", "needed_start": "2018-01-22", "needed_end": "2026-08-31",
         "reasons": "A2_mcap_rank400", "active": "Y", "successor_routed": "", "note": ""},
    ])
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    # Security 7 (another class on the same ticker) traded at other levels on two list dates.
    quotes = pd.DataFrame({"security_id": ["7", "7"], "as_of_session": ["2018-02-01", "2019-03-01"],
                           "last_sale": ["80", "81"], "as_of_check": ["confirmed", "confirmed"]})
    refs = _Refs([MASTER_ROW, {**MASTER_ROW, "security_id": "7"}], quotes=quotes)
    summary = yh.build(requests, refs=refs, raw_dir=raw, out_dir=out, sessions=sessions,
                       metrics_path=tmp_path / "absent.pkl")
    assert summary["verdicts"] == {"ok": 1, "failed": 1, "wrong_entity": 1}
    assert (out / "rejected" / "7.csv.gz").exists() and not (out / "7.csv.gz").exists()
    series = pd.read_csv(out / "9.csv.gz")
    assert list(series.columns[:5]) == ["date", "close_raw", "volume_raw", "split_factor", "div_cash"]
    assert series["date"].iloc[0] == "2018-01-22" and series["date"].iloc[-1] == "2026-08-31"
    assert series.loc[series["date"] < days[100], "close_raw"].iloc[0] == pytest.approx(50.0)
    events = pd.read_csv(out / "events.csv")
    assert sorted(events.loc[events["security_id"] == 9, "event_type"]) == ["dividend", "split"]
    assert set(events.loc[events["security_id"] == 7, "verdict"]) == {"wrong_entity"}
    status = pd.read_csv(out / "fetch_status.csv").set_index("symbol")
    assert status.loc["GONE", "status"] == "not_found" and status.loc["TEST", "fetched_utc"] == "2026-10-01T01:02:03+00:00"
    report = pd.read_csv(out / "entity_report.csv").set_index("security_id")
    assert report.loc[8, "verdict"] == "failed"
    assert not (out / "8.csv.gz").exists()


def test_weekly_coverage_adds_yahoo_weeks_to_the_vendor_count(tmp_path):
    weeks = pd.to_datetime(["2019-01-04", "2019-01-11"])
    metrics = pd.DataFrame({
        "security_id": ["1", "2", "3", "1", "2", "3"], "week_end": list(weeks.repeat(3)),
        "universe": [True] * 6, "dv50_rank": [1.0, 2.0, 400.0, 1.0, 2.0, 3.0],
        "vendor_ok": [True, False, False, True, False, False]})
    path = tmp_path / "weekly.pkl"
    metrics.to_pickle(path)
    yahoo = {"2": {pd.Timestamp("2019-01-09").to_period("W-SUN")}}
    out = yh.weekly_coverage(yahoo, path).set_index("week_end")
    assert out.loc["2019-01-04", "top300"] == 2 and out.loc["2019-01-04", "vendor_after"] == 1
    assert out.loc["2019-01-11", "vendor_after"] == 2 and out.loc["2019-01-11", "stored_only_after"] == 1
    assert yh.coverage_by_year(out.reset_index())[2019]["after_max"] == 2


def test_a_steady_level_offset_is_a_restore_problem_for_review_not_another_entity():
    # Yahoo applied a 5% stock dividend it did not list: every restored close is 1/1.05 of the list price.
    quotes = pd.DataFrame({"security_id": ["9"] * 3, "as_of_session": ["2018-02-01", "2018-09-04", "2019-03-01"],
                           "last_sale": ["52.5", "52.5", "52.5"], "as_of_check": ["confirmed"] * 3})
    result = _check(refs=_Refs([MASTER_ROW], quotes=quotes))
    assert result["verdict"] == "review" and "level_offset_lastsale:2018-02-01..2019-03-01@0.9524" in result["verdict_reasons"]
    # The same miss with another name stays another entity.
    other = _check(meta={"longName": "Other Robotics Inc"}, refs=_Refs([MASTER_ROW], quotes=quotes))
    assert other["verdict"] == "wrong_entity"


def test_an_ipo_need_opened_before_the_first_trade_counts_as_covered():
    master = [{**MASTER_ROW, "first_listed": "2019-05-01"}]
    days = pd.bdate_range("2019-05-20", "2026-08-31")
    result = _check(meta={"firstTradeDate": _stamp("2019-05-20")}, days=days,
                    request={"needed_start": "2019-05-01"}, refs=_Refs(master))
    assert result["verdict"] == "ok" and "need_starts_before_first_trade:13" in result["verdict_reasons"]
    # Rows that start well after Yahoo's own first trade date are a real gap.
    gap = _check(meta={"firstTradeDate": _stamp("2019-05-20")}, days=pd.bdate_range("2020-01-02", "2026-08-31"),
                 request={"needed_start": "2019-05-01"}, refs=_Refs(master))
    assert gap["verdict"] == "partial"


def test_best_raw_prefers_the_body_with_more_rows(tmp_path):
    short = _payload(["2026-07-17"], [1.0], [1])
    full = _payload(["2026-07-15", "2026-07-16", "2026-07-17"], [1.0, 1.0, 1.0], [1, 1, 1])
    (tmp_path / "ZZZ__20261001T000000Z.json.gz").write_bytes(gzip.compress(json.dumps(full).encode()))
    (tmp_path / "ZZZ__20261002T000000Z.json.gz").write_bytes(gzip.compress(json.dumps(short).encode()))
    assert yh.best_raw("ZZZ", tmp_path).name == "ZZZ__20261001T000000Z.json.gz"


def test_retry_asks_cached_symbols_again_from_query2(tmp_path):
    first = _FakeGetter({})
    yh.fetch_symbols(["AAA"], period2="2026-10-02", raw_dir=tmp_path, getter=first)
    urls = []

    def getter(url, path, **kwargs):
        urls.append(url)
        return _FakeGetter({})(url, path, **kwargs)

    yh.fetch_symbols(["AAA"], period2="2026-10-02", raw_dir=tmp_path, getter=getter, retry=True)
    assert len(urls) == 1 and urls[0].startswith("https://query2.finance.yahoo.com/v8/finance/chart/AAA?")


def test_level_runs_find_an_adjustment_yahoo_made_without_listing_it():
    days = pd.bdate_range("2014-01-01", periods=200)
    daily = _daily(days, [90.42] * 120 + [100.0] * 80)
    wiki = pd.DataFrame({"date": days, "close": [100.0] * 200})
    wiki.loc[50, "close"] = 130.0  # one bad print does not split the run
    result = yh.wiki_check(daily, wiki)
    assert result["wiki_runs"] == 2 and result["wiki_piecewise_stable"]
    assert result["wiki_off_runs"] == f"2014-01-01..{days[119]:%Y-%m-%d}@0.9042"
    checked = _check(days=days, closes=[90.42] * 120 + [100.0] * 80, request={"needed_start": "2014-01-01",
                                                                             "needed_end": f"{days[-1]:%Y-%m-%d}"},
                     refs=_Refs([MASTER_ROW], wiki=wiki))
    assert checked["verdict"] == "review" and "level_offset_wiki:2014-01-01" in checked["verdict_reasons"]


def test_split_day_flags_skip_events_that_wiki_already_verified():
    days = ["2025-05-05", "2025-05-06", "2025-05-07", "2025-05-08"]
    odd = _payload(days, [900.0, 900.0, 75.0, 90.0], [1, 1, 1, 1], splits=[("2025-05-07", 1, 20)])
    _, daily, events = yh.parse_chart(odd)
    assert yh.split_restore_flags(daily, events, pd.Timestamp("2025-05-08")) == []


def test_symbol_overrides_replace_the_candidate_ticker_and_say_why():
    candidates = pd.DataFrame([
        {"security_id": "5", "ticker_for_source": "OLDT", "needed_start": "2011-06-01", "needed_end": "2026-08-31",
         "reason": "V_verify_sample", "planned_source": "tiingo", "active": "Y", "note": ""}])
    rows = yh.request_rows(candidates, overrides={"5": ("NEWT", "the SPAC ticker is another company now")})
    assert rows.loc[0, "symbol"] == "NEWT" and rows.loc[0, "candidate_symbol"] == "OLDT"
    assert "symbol OLDT -> NEWT" in rows.loc[0, "note"]


def test_y_active_all_rows_are_requested_and_a_relisted_name_asks_yahoo_only_after_its_form25():
    # SMCI (round 5): the need before the 2019 Form 25 is a Tiingo row, the later listing a Yahoo row.
    candidates = pd.DataFrame([
        {"security_id": "1375365", "ticker_for_source": "SMCI", "needed_start": "2018-01-21", "needed_end": "2018-09-06",
         "reason": "B_A_float_ge_1B", "planned_source": "tiingo", "active": "N", "note": "the need before"},
        {"security_id": "1375365", "ticker_for_source": "SMCI", "needed_start": "2019-11-01", "needed_end": "2026-08-31",
         "reason": "Y_active_rank300", "planned_source": "yahoo", "active": "Y", "note": "listed again"},
        {"security_id": "9", "ticker_for_source": "QUIET", "needed_start": "2015-01-02", "needed_end": "2026-08-31",
         "reason": "Y_active_all", "planned_source": "yahoo", "active": "Y", "note": ""}])
    rows = yh.request_rows(candidates, overrides={}, segments={}).set_index("security_id")
    assert rows.loc["1375365", "symbol"] == "SMCI" and rows.loc["1375365", "needed_start"] == "2019-11-01"
    assert rows.loc["1375365", "reasons"] == "Y_active_rank300"
    assert rows.loc["9", "symbol"] == "QUIET" and rows.loc["9", "reasons"] == "Y_active_all"


def test_a_reused_ticker_is_trimmed_to_the_security_own_span():
    spans = pd.DataFrame([
        {"security_id": "old", "ticker": "TEST", "list_start": "2015-12-03", "list_end": "2020-07-11",
         "obs_end": "2020-06-29"},
        {"security_id": "9", "ticker": "TEST", "list_start": "2020-06-30", "list_end": "2026-08-31", "obs_end": ""},
    ])
    refs = _Refs([MASTER_ROW])
    refs.spans = spans
    assert refs.own_ticker_window("9", "TEST") == ("2020-06-30", "2026-08-31", "ticker TEST held by old until 2020-06-29")
    assert refs.own_ticker_window("old", "TEST") is None


def test_a_distribution_served_as_an_odd_ratio_is_always_treated_as_applied():
    # Liberty's 2016 recapitalisation: the adjusted close still falls by about 1/1.424.
    days = ["2016-04-14", "2016-04-15", "2016-04-18", "2016-04-19"]
    payload = _payload(days, [26.0, 26.26, 18.22, 18.22], [1, 1, 1, 1], splits=[("2016-04-18", 1423988, 1000000)])
    _, daily, events = yh.parse_chart(payload)
    assert events.loc[0, "applied_by_yahoo"] == "Y"
    assert daily["close_raw"].iloc[1] == pytest.approx(26.26 * 1.423988)


def test_another_share_class_fails_even_when_half_the_list_prices_agree():
    # Class B trades near class A for a while, then at wandering premiums.
    days = pd.bdate_range("2014-01-01", "2019-06-10", freq="20B")
    prices = [50.0 if k < 0.6 * len(days) else 50.0 * (1.08 + 0.05 * (k % 3)) for k in range(len(days))]
    quotes = pd.DataFrame({"security_id": "9", "as_of_session": days.strftime("%Y-%m-%d"),
                           "last_sale": [str(v) for v in prices], "as_of_check": "confirmed"})
    result = _check(days=pd.bdate_range("2014-01-01", "2026-08-31"), request={"needed_start": "2014-01-01"},
                    refs=_Refs([MASTER_ROW], quotes=quotes))
    assert result["lastsale_agree"] >= 0.5 * result["lastsale_n"]
    assert result["verdict"] == "wrong_entity" and "lastsale_level" in result["verdict_reasons"]


def test_a_short_wiki_file_does_not_overrule_many_agreeing_list_prices():
    days = pd.bdate_range("2015-02-09", "2026-08-31")
    quote_days = pd.bdate_range("2015-03-02", "2019-06-03", freq="40B")
    quotes = pd.DataFrame({"security_id": "9", "as_of_session": quote_days.strftime("%Y-%m-%d"),
                           "last_sale": "50.0", "as_of_check": "confirmed"})
    wiki = pd.DataFrame({"date": days[140:178], "close": [47.0, 53.5] * 19})  # an unsteady short file
    result = _check(days=days, request={"needed_start": "2015-02-09"},
                    refs=_Refs([MASTER_ROW], quotes=quotes, wiki=wiki))
    assert result["verdict"] == "review" and "wiki_short_disagrees:38d" in result["verdict_reasons"]


def test_segments_join_two_symbols_into_one_security_file(tmp_path):
    candidates = pd.DataFrame([
        {"security_id": "9", "ticker_for_source": "NEWT", "needed_start": "2018-01-22", "needed_end": "2026-08-31",
         "reason": "A1_wiki_dv_rank300", "planned_source": "yahoo", "active": "Y", "note": ""}])
    segments = {"9": [("OLDT", "", "2020-06-30", "old history"), ("NEWT", "2020-07-01", "", "after the split-off")]}
    requests = yh.request_rows(candidates, segments=segments)
    assert list(requests["symbol"]) == ["OLDT", "NEWT"]
    assert list(requests["needed_end"]) == ["2020-06-30", "2026-08-31"]
    assert list(requests["needed_start"]) == ["2018-01-22", "2020-07-01"]
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    days = list(pd.bdate_range("2018-01-22", "2026-08-31").strftime("%Y-%m-%d"))
    for symbol, level in (("OLDT", 10.0), ("NEWT", 20.0)):
        payload = _payload(days, [level] * len(days), [1000] * len(days))
        (raw / f"{symbol}__20261001T010203Z.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    summary = yh.build(requests, refs=_Refs([MASTER_ROW]), raw_dir=raw, out_dir=out, sessions=sessions,
                       metrics_path=tmp_path / "absent.pkl")
    assert summary["verdicts"] == {"ok": 2}
    series = pd.read_csv(out / "9.csv.gz")
    assert series["date"].is_monotonic_increasing and series["date"].is_unique
    assert set(series.loc[series["date"] <= "2020-06-30", "symbol"]) == {"OLDT"}
    assert set(series.loc[series["date"] >= "2020-07-01", "close_raw"]) == {20.0}


def test_segment_junction_is_marked():
    candidates = pd.DataFrame([
        {"security_id": "9", "ticker_for_source": "NEWT", "needed_start": "2020-06-01", "needed_end": "2020-08-31",
         "reason": "A1_wiki_dv_rank300", "planned_source": "yahoo", "active": "Y", "note": ""}])
    segments = {"9": [("OLDT", "", "2020-06-30", "old"), ("NEWT", "2020-07-01", "", "new")]}
    requests = yh.request_rows(candidates, segments=segments)
    assert len(requests) == 2
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        raw, out = Path(tmp) / "raw", Path(tmp) / "out"
        raw.mkdir()
        days = list(pd.bdate_range("2020-06-01", "2020-08-31").strftime("%Y-%m-%d"))
        for symbol in ("OLDT", "NEWT"):
            payload = _payload(days, [10.0] * len(days), [1000] * len(days), splits=[("2020-07-01", 3, 1)])
            (raw / f"{symbol}__20261001T010203Z.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
        sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
        yh.build(requests, refs=_Refs([MASTER_ROW]), raw_dir=raw, out_dir=out, sessions=sessions,
                 metrics_path=Path(tmp) / "absent.pkl")
        series = pd.read_csv(out / "9.csv.gz", keep_default_na=False)
        assert list(series.loc[series["junction"] == "Y", "date"]) == ["2020-07-01"]
        events = pd.read_csv(out / "events.csv", keep_default_na=False)
        # OLDT's split on 2020-07-01 is outside its segment; NEWT's is kept, flagged as a junction event
        # (its S comes from NEWT's history before the segment) and named in the verdict reasons.
        assert list(events["symbol"]) == ["NEWT"] and events.loc[0, "flags"] == "segment_junction"
        report = pd.read_csv(out / "entity_report.csv", keep_default_na=False).set_index("symbol")
        assert "segment_junction_event:2020-07-01:split:3" in report.loc["NEWT", "verdict_reasons"]
        assert report.loc["NEWT", "verdict"] == "review" and report.loc["OLDT", "verdict"] == "ok"


def test_a_split_after_the_last_row_is_judged_against_the_stored_file():
    days = list(pd.bdate_range("2026-06-01", "2026-09-11").strftime("%Y-%m-%d"))
    payload = _payload(days, [10.0] * len(days), [1000] * len(days), splits=[("2026-09-14", 1, 50)])
    _, daily, events = yh.parse_chart(payload)
    assert events.loc[0, "on_session"] == "N" and daily["close_raw"].iloc[0] == pytest.approx(0.2)
    stored = pd.DataFrame({"date": pd.to_datetime(days), "close": 10.1, "volume": 1000})
    late = yh.unsessioned_splits_not_applied(daily, events, stored)
    assert late == {pd.Timestamp("2026-09-14")}
    _, daily, events = yh.parse_chart(payload, not_applied=late)
    assert daily["close_raw"].iloc[0] == pytest.approx(10.0) and events.loc[0, "applied_by_yahoo"] == "N"
    # Stored closes in post-split units (500) mean Yahoo did apply it.
    assert yh.unsessioned_splits_not_applied(daily, events, stored.assign(close=500.0)) == set()


def test_dividends_are_restored_with_every_listed_split_even_one_not_applied_to_prices():
    days = list(pd.bdate_range("2026-06-01", "2026-09-11").strftime("%Y-%m-%d"))
    payload = _payload(days, [10.0] * len(days), [1000] * len(days), splits=[("2026-09-14", 1, 50)],
                       dividends=[(days[10], 5.0)])  # a $0.10 dividend served x50
    _, daily, events = yh.parse_chart(payload, not_applied={pd.Timestamp("2026-09-14")})
    assert daily["close_raw"].iloc[0] == pytest.approx(10.0)
    assert events.loc[events["event_type"] == "dividend", "div_cash_raw"].iloc[0] == pytest.approx(0.1)


# ------------------------------------------------------------------ round-2 fixes

def _wiki(days, volumes):
    return pd.DataFrame({"date": pd.to_datetime(days), "close": 10.0, "volume": volumes})


def test_a_distribution_whose_volume_yahoo_left_raw_keeps_the_served_volume():
    # PENN 2013-11-04 (4.423): Yahoo scaled close by the ratio but served the raw volume before it.
    days = list(pd.bdate_range("2013-08-01", "2013-12-31").strftime("%Y-%m-%d"))
    ex = "2013-11-04"
    closes = [58.0 / 4.423 if d < ex else 13.75 for d in days]
    volumes = [2_400_000] * len(days)
    payload = _payload(days, closes, volumes, splits=[(ex, 4423, 1000)])
    _, daily, events = yh.parse_chart(payload)
    assert daily.loc[0, "volume_raw"] == pytest.approx(2_400_000 / 4.423, abs=1)  # the old restore
    position = days.index(ex)
    wiki = _wiki(days[position - 60:position], [2_400_000] * 60)  # WIKI raw volume before the event
    as_served, evidence = yh.volume_scaling(daily, events, wiki)
    assert as_served == {pd.Timestamp(ex)} and evidence[pd.Timestamp(ex)] == "wiki:60:1.0000:0.2261"
    _, daily, events = yh.parse_chart(payload, volume_as_served=as_served, volume_evidence=evidence)
    assert daily.loc[0, "volume_raw"] == 2_400_000 and daily.loc[0, "volume_cum_after"] == 1.0
    assert daily.loc[0, "close_raw"] == pytest.approx(58.0) and daily.loc[0, "split_cum_after"] == pytest.approx(4.423)
    flagged = yh.event_flags(events, daily)
    assert flagged.loc[0, "volume_restored"] == "N"
    assert flagged.loc[0, "flags"] == "odd_ratio volume_not_scaled_by_yahoo"


def test_a_distribution_whose_volume_yahoo_scaled_stays_in_the_volume_restore():
    # EBAY 2015-07-20 (2.376): the served volume before the event is the raw volume x 2.376.
    days = list(pd.bdate_range("2015-04-01", "2015-09-30").strftime("%Y-%m-%d"))
    ex = "2015-07-20"
    payload = _payload(days, [30.0] * len(days), [1_000_000 * 2.376 if d < ex else 1_000_000 for d in days],
                       splits=[(ex, 2376, 1000)])
    _, daily, events = yh.parse_chart(payload)
    position = days.index(ex)
    wiki = _wiki(days[position - 60:position], [1_000_000] * 60)
    as_served, evidence = yh.volume_scaling(daily, events, wiki)
    assert as_served == set() and evidence[pd.Timestamp(ex)] == "wiki:60:1.0000:2.3760"
    _, daily, events = yh.parse_chart(payload, volume_as_served=as_served, volume_evidence=evidence)
    assert daily.loc[0, "volume_raw"] == 1_000_000
    flagged = yh.event_flags(events, daily)
    assert flagged.loc[0, "flags"] == "odd_ratio" and flagged.loc[0, "volume_restored"] == "Y"


def test_volume_checks_go_latest_first_and_need_wiki_inside_the_60_sessions_before():
    # Two distributions: Yahoo left volume raw for the 2014 one and scaled it for the 2016 one.
    days = list(pd.bdate_range("2013-10-01", "2016-12-30").strftime("%Y-%m-%d"))
    early, late = "2014-03-04", "2016-06-21"
    served = [1000 * (1.146 if d < late else 1.0) for d in days]  # 2014's ratio not applied to volume
    payload = _payload(days, [20.0] * len(days), served, splits=[(early, 1957, 1000), (late, 1146, 1000)])
    _, daily, events = yh.parse_chart(payload)
    wiki = _wiki([d for d in days if d < "2016-12-01"], 1000)
    as_served, evidence = yh.volume_scaling(daily, events, wiki)
    assert as_served == {pd.Timestamp(early)}
    assert evidence[pd.Timestamp(late)].startswith("wiki:60:1.0000") and evidence[pd.Timestamp(early)].startswith("wiki:60:1.0000")
    _, restored, _ = yh.parse_chart(payload, volume_as_served=as_served)
    assert set(restored["volume_raw"]) == {1000}
    # WIKI only long before the event is no evidence: unverified; a reverse split 1:150 is a share split.
    old_wiki = _wiki(days[:40], 1000)
    payload = _payload(days, [20.0] * len(days), served, splits=[(late, 1146, 1000), ("2016-11-01", 1, 150)])
    _, daily, events = yh.parse_chart(payload)
    as_served, evidence = yh.volume_scaling(daily, events, old_wiki)
    assert as_served == set() and evidence == {pd.Timestamp(late): "none", pd.Timestamp("2016-11-01"): "reverse_split"}
    _, daily, events = yh.parse_chart(payload, volume_evidence=evidence)
    flags = yh.event_flags(events, daily).set_index("ex_date")["flags"]
    assert flags[pd.Timestamp(late)] == "odd_ratio volume_restore_unverified" and flags[pd.Timestamp("2016-11-01")] == "odd_ratio"


def test_an_hourly_body_is_never_used_as_daily_bars(tmp_path):
    days = ["2026-07-17", "2026-08-24", "2026-08-25"]
    daily_body = _payload(days, [83.86, 84.78, 84.86], [5_373_700, 908_511, 1_241_642], meta={"dataGranularity": "1d"})
    hourly = _payload(days * 3, [83.83] * 9, [657_669] * 9, meta={"dataGranularity": "1h"})
    result = hourly["chart"]["result"][0]
    result["timestamp"] = sorted(_stamp(d, h) for d in days for h in (13, 14, 15))
    (tmp_path / "CRNX__20261001T125158Z.json.gz").write_bytes(gzip.compress(json.dumps(daily_body).encode()))
    (tmp_path / "CRNX__20261001T132051Z.json.gz").write_bytes(gzip.compress(json.dumps(hourly).encode()))
    assert yh.bar_granularity(hourly) == "1h" and yh.bar_granularity(daily_body) == "1d"
    assert yh.best_raw("CRNX", tmp_path).name == "CRNX__20261001T125158Z.json.gz"  # fewer stamps, but daily
    meta, daily, events = yh.parse_chart(hourly)
    assert daily.empty and events.empty and meta["error"] == "not daily bars (1h)"
    # Without dataGranularity, two bars on one New York date are not daily bars.
    del result["meta"]["dataGranularity"]
    assert yh.bar_granularity(hourly) == "intraday"
    _, daily, _ = yh.parse_chart(daily_body)
    assert list(daily["volume_raw"]) == [5_373_700, 908_511, 1_241_642]


def test_retry_can_ask_from_the_need_start(tmp_path):
    urls = []

    def getter(url, path, **kwargs):
        urls.append(url)
        return _FakeGetter({})(url, path, **kwargs)

    yh.fetch_symbols(["CRNX"], period1="2018-07-18", period2="2026-10-03", raw_dir=tmp_path, getter=getter, retry=True)
    assert len(urls) == 1 and "period1=1531872000" in urls[0] and "interval=1d" in urls[0]
    assert urls[0].startswith("https://query2.finance.yahoo.com/")


def test_claimed_rows_are_cut_on_the_side_of_the_claimant_only():
    master = [{**MASTER_ROW, "security_id": "new", "multi_class_group": ""},
              {**MASTER_ROW, "security_id": "old", "successor_security_id": "new", "multi_class_group": ""},
              {**MASTER_ROW, "security_id": "classA", "multi_class_group": "G"},
              {**MASTER_ROW, "security_id": "classC", "multi_class_group": "G"}]
    spans = pd.DataFrame([
        {"security_id": "old", "ticker": "GOOG", "list_start": "2010-12-31", "list_end": "2015-10-08", "obs_end": ""},
        {"security_id": "new", "ticker": "GOOGL", "list_start": "2015-10-09", "list_end": "2026-08-31", "obs_end": ""},
        {"security_id": "classA", "ticker": "LMCA", "list_start": "2013-01-10", "list_end": "2017-01-25", "obs_end": ""},
        {"security_id": "classC", "ticker": "LMCK", "list_start": "2014-08-13", "list_end": "2017-01-25", "obs_end": ""},
        {"security_id": "later", "ticker": "LMCK", "list_start": "2019-01-02", "list_end": "2026-08-31", "obs_end": ""},
        {"security_id": "solo", "ticker": "SOLO", "list_start": "2020-06-01", "list_end": "2026-08-31", "obs_end": ""},
    ])
    refs = _Refs(master)
    refs.spans = spans
    # A predecessor: rows before the successor's span go; no claimant after it, so nothing is cut there.
    assert refs.claimed_cut("new", "GOOGL", "2018-01-21", "2026-08-31") == {
        "first": "2015-10-09", "last": "", "before": "predecessor:old", "after": ""}
    # A sibling class issued earlier, and a later holder of the ticker; the need is never cut.
    cut = refs.claimed_cut("classC", "LMCK", "2014-07-08", "2017-01-25", same_symbol={"classC", "later"})
    assert (cut["first"], cut["last"]) == ("2014-07-08", "2017-01-25")
    assert cut["before"] == "sibling:classA" and cut["after"] == "same_symbol:later ticker:later"
    # Nobody else on either side (an exchange move): every row is kept.
    assert refs.claimed_cut("solo", "SOLO", "2020-06-01", "2026-08-31")["first"] == ""


def test_build_cuts_claimed_rows_flags_the_junction_and_rejects_a_series_outside_its_need(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    days = list(pd.bdate_range("2016-01-04", "2026-08-31").strftime("%Y-%m-%d"))
    payload = _payload(days, [10.0] * len(days), [1000] * len(days), splits=[("2018-01-22", 3054, 1000)])
    (raw / "TEST__20261001T010203Z.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
    later = list(pd.bdate_range("2025-09-29", "2026-08-31").strftime("%Y-%m-%d"))
    (raw / "WOLF__20261001T010203Z.json.gz").write_bytes(
        gzip.compress(json.dumps(_payload(later, [20.0] * len(later), [1000] * len(later))).encode()))
    master = [MASTER_ROW, {**MASTER_ROW, "security_id": "8", "successor_security_id": "9"},
              {**MASTER_ROW, "security_id": "7"}]
    refs = _Refs(master)
    refs.spans = pd.DataFrame([
        {"security_id": "8", "ticker": "OLD", "list_start": "2011-01-03", "list_end": "2018-01-19", "obs_end": ""},
        {"security_id": "9", "ticker": "TEST", "list_start": "2018-01-22", "list_end": "2026-08-31", "obs_end": ""}])
    (out / "rejected").mkdir(parents=True)
    (out / "6.csv.gz").write_bytes(b"")  # a security the candidate list no longer asks for
    requests = pd.DataFrame([
        {"security_id": "9", "symbol": "TEST", "needed_start": "2018-01-22", "needed_end": "2026-08-31",
         "reasons": "Y_active_rank300", "active": "Y", "successor_routed": "", "note": ""},
        {"security_id": "7", "symbol": "WOLF", "needed_start": "2018-01-21", "needed_end": "2021-10-31",
         "reasons": "A1_wiki_dv_rank300", "active": "N", "successor_routed": "", "note": ""}])
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    summary = yh.build(requests, refs=refs, raw_dir=raw, out_dir=out, sessions=sessions,
                       metrics_path=tmp_path / "absent.pkl")
    assert summary["verdicts"] == {"review": 1, "no_rows": 1} and summary["moved_to_not_requested"] == ["6"]
    assert (out / "not_requested" / "6.csv.gz").exists() and not (out / "6.csv.gz").exists()
    series = pd.read_csv(out / "9.csv.gz", keep_default_na=False)
    assert series["date"].iloc[0] == "2018-01-22" and series.loc[0, "junction"] == "Y"
    report = pd.read_csv(out / "entity_report.csv", keep_default_na=False).set_index("security_id")
    reasons = report.loc[9, "verdict_reasons"]
    assert "claimed_rows_cut:" in reasons and "before 2018-01-22 (predecessor:8)" in reasons
    assert "trim_junction_event:2018-01-22:split:3.054" in reasons
    events = pd.read_csv(out / "events.csv", keep_default_na=False)
    assert "trim_junction" in events.loc[events["security_id"] == 9, "flags"].iloc[0]
    # Rows, but none in the need (post-bankruptcy equity under the old ticker): no_rows, rejected.
    assert report.loc[7, "verdict"] == "no_rows" and report.loc[7, "verdict_reasons"].startswith("no rows in the need")
    assert (out / "rejected" / "7.csv.gz").exists() and not (out / "7.csv.gz").exists()


def test_a_wiki_file_step6_found_to_be_another_company_is_no_reference(tmp_path):
    # 22701 (SUNation, ex Communications Systems) maps SunEdison's SUNE.csv.gz by continuity; step 6 dropped it.
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    pd.DataFrame({"ticker": ["SUNE", "SUNE"], "date": ["2015-01-02", "2015-01-05"], "close": [20.0, 21.0],
                  "volume": [1e6, 1e6]}).to_csv(wiki / "SUNE.csv.gz", index=False)
    files = pd.DataFrame([{"security_id": "22701", "src": "wiki", "file": "SUNE.csv.gz", "first": "2011-06-01",
                           "last": "2016-04-21", "rows": "2", "direct": "0.0", "entity_check_failed": "True"}])
    refs = yh.References(pd.DataFrame([{**MASTER_ROW, "security_id": "22701"}]),
                         pd.DataFrame(columns=["security_id", "name_in_source"]),
                         pd.DataFrame(columns=["security_id", "as_of_session", "last_sale", "as_of_check"]),
                         files, wiki_dir=wiki, stored_dir=tmp_path)
    assert refs.wiki("22701").empty
    files["entity_check_failed"] = "False"
    refs.files = {sid: g for sid, g in files.groupby("security_id")}
    assert len(refs.wiki("22701")) == 2


def test_an_empty_body_is_a_failed_series_not_a_crash(tmp_path):
    # AGEND (round 5): Yahoo answered a reverse-split ticker with an ECNQUOTE body and no bars.
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    payload = _payload([], [], [], meta={"symbol": "AGEND", "instrumentType": "ECNQUOTE", "dataGranularity": "1d"})
    (raw / "AGEND__20261002T010203Z.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
    refs = _Refs([MASTER_ROW])
    requests = pd.DataFrame([{"security_id": "9", "symbol": "AGEND", "needed_start": "2018-01-21",
                              "needed_end": "2026-08-31", "reasons": "Y_active_all", "active": "Y",
                              "successor_routed": "", "note": ""}])
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    summary = yh.build(requests, refs=refs, raw_dir=raw, out_dir=out, sessions=sessions,
                       metrics_path=tmp_path / "absent.pkl")
    assert summary["fetch_status"] == {"empty": 1}
    report = pd.read_csv(out / "entity_report.csv", keep_default_na=False)
    assert report.loc[0, "verdict"] not in yh.ACCEPTED and not (out / "9.csv.gz").exists()


def test_series_files_keep_small_factors_and_integer_volumes(tmp_path):
    # Repeated reverse splits leave a cumulative factor below 5e-7, which %.6f wrote as 0.000000.
    days = ["2020-01-02", "2020-01-03", "2021-01-04", "2022-01-03"]
    payload = _payload(days, [40000.0, 41000.0, 30.0, 25.0], [7, 9, 120_000, 130_000],
                       splits=[("2021-01-04", 1, 1500), ("2022-01-03", 1, 1500)])
    _, daily, _ = yh.parse_chart(payload)
    frame = daily.assign(date=daily["date"].dt.strftime("%Y-%m-%d"), symbol="KUST", junction="")
    written = pd.read_csv(io.BytesIO(yh.series_csv(frame)), dtype={"volume_raw": str})
    assert written.loc[0, "split_cum_after"] == pytest.approx(1 / 1500 ** 2, rel=1e-9)
    assert np.allclose(written["close_yahoo"] * written["split_cum_after"], written["close_raw"], rtol=1e-9)
    assert written.loc[0, "volume_raw"] == "15750000"  # 7 x 1500^2, an integer, not 1.575e+07


def test_a_steady_offset_inside_the_lastsale_band_is_reported():
    # FWONK: every list price is 0.9833 of the raw close, inside the 2% band, so all agree.
    quote_days = pd.bdate_range("2018-02-01", "2019-06-03", freq="20B")
    quotes = pd.DataFrame({"security_id": "9", "as_of_session": quote_days.strftime("%Y-%m-%d"),
                           "last_sale": str(50.0 / 0.9833), "as_of_check": "confirmed"})
    result = _check(refs=_Refs([MASTER_ROW], quotes=quotes))
    assert result["lastsale_agree"] == result["lastsale_n"]
    expected = f"level_offset_lastsale:2018-02-01..{quote_days[-1]:%Y-%m-%d}@0.9833"
    assert result["verdict"] == "review" and expected in result["verdict_reasons"]
    # A level within 1% is not reported.
    quotes["last_sale"] = str(50.0 / 0.995)
    assert _check(refs=_Refs([MASTER_ROW], quotes=quotes))["verdict"] == "ok"


def test_need_sessions_missing_between_the_first_and_last_row_make_a_series_partial():
    days = pd.bdate_range("2018-01-22", "2026-08-31")
    gappy = days[(days < "2026-07-20") | (days > "2026-08-21")]
    result = _check(days=gappy)
    assert result["verdict"] == "partial" and result["missing_inside"] == 25
    assert "missing_inside:25 need sessions" in result["verdict_reasons"]


# ------------------------------------------------------------------ round 6

def test_request_rows_carry_sec_current_tickers_only_where_the_candidate_ticker_is_not_one():
    master = pd.DataFrame([
        {"security_id": "1335105", "tickers_sec_current": "NMAD", "exchanges_sec_current": "Nasdaq", "multi_class_group": ""},
        {"security_id": "5", "tickers_sec_current": "SAME SAMEW SAMEL", "exchanges_sec_current": "Nasdaq Nasdaq Nasdaq",
         "multi_class_group": ""},
        {"security_id": "1560385.T-LLYVA", "tickers_sec_current": "FWONA FWONK", "exchanges_sec_current": "Nasdaq Nasdaq",
         "multi_class_group": "1560385"}])
    alternates = yh.sec_alternates(master)
    assert alternates == {"1335105": ["NMAD"], "5": ["SAME"]}
    candidates = pd.DataFrame([
        {"security_id": "1335105", "ticker_for_source": "LIXT", "needed_start": "2020-11-25", "needed_end": "2026-08-31",
         "reason": "Y_active_all", "planned_source": "yahoo", "active": "Y", "note": ""},
        {"security_id": "5", "ticker_for_source": "SAME", "needed_start": "2020-11-25", "needed_end": "2026-08-31",
         "reason": "Y_active_all", "planned_source": "yahoo", "active": "Y", "note": "",
         "junction_date": "2021-01-04"}])
    rows = yh.request_rows(candidates, overrides={}, segments={}, alternates=alternates).set_index("security_id")
    assert rows.loc["1335105", "alt_symbols"] == "NMAD" and rows.loc["5", "alt_symbols"] == ""
    assert rows.loc["5", "junction_date"] == "2021-01-04" and rows.loc["1335105", "junction_date"] == ""


def test_a_404_snapshot_ticker_is_replaced_by_the_sec_current_ticker(tmp_path):
    # Lixte (round 6): the last Nasdaq snapshot's LIXT came back 404; SEC lists NMAD, which Yahoo serves.
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    (raw / "LIXT__20261001T010203Z.json.gz.404").write_bytes(b"")
    days = list(pd.bdate_range("2020-11-25", "2026-08-31").strftime("%Y-%m-%d"))
    (raw / "NMAD__20261002T010203Z.json.gz").write_bytes(
        gzip.compress(json.dumps(_payload(days, [5.0] * len(days), [1000] * len(days))).encode()))
    requests = pd.DataFrame([{"security_id": "9", "symbol": "LIXT", "candidate_symbol": "LIXT",
                              "needed_start": "2020-11-25", "needed_end": "2026-08-31", "reasons": "Y_active_all",
                              "active": "Y", "successor_routed": "", "note": "", "alt_symbols": "NMAD",
                              "junction_date": ""}])
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    assert list(yh.poor_answers(requests, sessions, raw)) == [0]
    summary = yh.build(requests, refs=_Refs([MASTER_ROW]), raw_dir=raw, out_dir=out, sessions=sessions,
                       metrics_path=tmp_path / "absent.pkl")
    report = pd.read_csv(out / "entity_report.csv", keep_default_na=False).iloc[0]
    assert (report["symbol"], report["candidate_symbol"], report["verdict"]) == ("NMAD", "LIXT", "ok")
    assert "symbol LIXT answered 404; the SEC current ticker NMAD" in report["note"]
    status = pd.read_csv(out / "fetch_status.csv", keep_default_na=False).set_index("symbol")
    assert status.loc["LIXT", "status"] == "not_found" and "replaced by NMAD" in status.loc["LIXT", "message"]
    assert summary["symbols_replaced_by_sec_ticker"] == {"LIXT": status.loc["LIXT", "message"]}
    # a good answer is kept even where an SEC ticker differs
    (raw / "OKAY__20261001T010203Z.json.gz").write_bytes(
        gzip.compress(json.dumps(_payload(days, [5.0] * len(days), [1000] * len(days))).encode()))
    good = requests.assign(symbol="OKAY", candidate_symbol="OKAY")
    assert yh.resolve_alternates(good, sessions, raw)[0].loc[0, "symbol"] == "OKAY"


def test_a_relisting_is_a_junction_and_a_10x_move_on_no_split_day_is_reviewed(tmp_path):
    # Oasis/Chord (round 6): Yahoo's CHRD joins old Oasis at $0.12 (to 2020-11-19) to the new equity at $34
    # (from 2020-11-20) with no split; the candidate list names the relisting day.
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    old = list(pd.bdate_range("2020-09-01", "2020-11-19").strftime("%Y-%m-%d"))
    new = list(pd.bdate_range("2020-11-20", "2021-03-31").strftime("%Y-%m-%d"))
    jump = ["2021-02-01"]  # and a later 20x print inside the need with no split
    closes = [0.12] * len(old) + [34.0] * len(new)
    closes[len(old) + new.index(jump[0])] = 700.0
    (raw / "CHRD__20261001T010203Z.json.gz").write_bytes(
        gzip.compress(json.dumps(_payload(old + new, closes, [1000] * len(closes))).encode()))
    requests = pd.DataFrame([{"security_id": "9", "symbol": "CHRD", "candidate_symbol": "CHRD",
                              "needed_start": "2020-11-20", "needed_end": "2021-03-31", "reasons": "Y_active_rank300",
                              "active": "Y", "successor_routed": "", "note": "", "alt_symbols": "",
                              "junction_date": "2020-11-20"}])
    sessions = pd.DatetimeIndex(pd.bdate_range("2011-06-01", "2026-08-31"))
    summary = yh.build(requests, refs=_Refs([MASTER_ROW]), raw_dir=raw, out_dir=out, sessions=sessions,
                       metrics_path=tmp_path / "absent.pkl")
    series = pd.read_csv(out / "9.csv.gz", keep_default_na=False)
    assert list(series.loc[series["junction"] == "Y", "date"]) == ["2020-11-20"]
    report = pd.read_csv(out / "entity_report.csv", keep_default_na=False).iloc[0]
    assert report["verdict"] == "review"
    assert "relist_junction:2020-11-20" in report["verdict_reasons"]
    assert "level_jump_not_a_split:2021-02-01" in report["verdict_reasons"]
    assert "2020-11-20" not in report["verdict_reasons"].split("level_jump_not_a_split:")[1]
    assert report["level_jumps"].startswith("2020-11-20:x283.3")
    assert summary["relist_junctions"] == ["relist_junction:2020-11-20"]
    # a split day is no level jump
    days = pd.to_datetime(["2021-01-04", "2021-01-05"])
    window = pd.DataFrame({"date": days, "close_raw": [100.0, 5.0], "split_factor": [1.0, 0.05]})
    assert yh.level_jumps(window, pd.DataFrame(columns=["event_type", "ex_date"])) == []


def test_the_request_ledger_matches_the_quota_ledger(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "AAA__20261001T010203Z.json.gz").write_bytes(b"x")
    (raw / "GONE__20261001T010205Z.json.gz.404").write_bytes(b"")
    (raw / "AGEND__20261002T002718Z.json.gz").write_bytes(b"x")
    ledger = tmp_path / "ledger.csv"
    pd.DataFrame([("2026-10-01T01:02:03+00:00", "yahoo", "2026-10", "AAA", "200"),
                  ("2026-10-01T01:02:05+00:00", "yahoo", "2026-10", "GONE", "404"),
                  ("2026-10-02T00:27:18+00:00", "yahoo", "2026-10", "AGEND", "200"),
                  ("2026-10-02T00:30:00+00:00", "yahoo", "2026-10", "CVX", "200"),
                  ("2026-10-02T00:31:00+00:00", "tiingo", "2026-10", "ZZZ", "200")],
                 columns=["fetched_utc", "source", "month", "symbol", "status"]).to_csv(ledger, index=False)
    index = tmp_path / "index.csv.gz"
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{}?period1=1"
    pd.DataFrame([("2026-10-01T01:02:03+00:00", "yahoo", url.format("AAA"), "200", str(raw / "AAA__x.json.gz")),
                  ("2026-10-01T01:02:05+00:00", "yahoo", url.format("GONE"), "404", ""),
                  ("2026-10-02T00:30:00+00:00", "yahoo", url.format("CVX"), "200", str(tmp_path / "terminal" / "CVX.json.gz"))],
                 columns=["fetched_utc", "source", "url_redacted", "http_status", "cache_path"]).to_csv(index, index=False)
    out = yh.request_ledger({"AAA", "GONE"}, raw, ledger, index)
    assert out["ledger_requests"] == 4 and out["this_step"]["requests"] == 3 and out["other_steps"]["requests"] == 1
    assert out["this_step"]["symbols_asked_not_in_this_build"] == {"AGEND": "2026-10-02T00:27:18+00:00"}
    assert out["raw_index_rows_missing"]["rows"] == ["AGEND@2026-10-02T00:27:18+00:00"]
    assert out["checks"]["ledger_this_step_plus_other_equals_ledger"] and not out["checks"]["raw_index_complete"]
