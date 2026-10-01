import gzip
from pathlib import Path
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from scripts import reversal_data_common as common
from scripts import reversal_data_wiki as wiki

HEADER = ",".join(wiki.COLUMNS)


def _row(ticker, date, close, volume=1000.0, dividend=0.0, split=1.0, adj=None, open_=None, high=None, low=None):
    adj = close if adj is None else adj
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    factor = adj / close
    return [ticker, date, open_, high, low, close, volume, dividend, split,
            open_ * factor, high * factor, low * factor, adj, volume / factor]


def _frame(rows):
    return pd.DataFrame(rows, columns=wiki.COLUMNS)


def _adjusted_series(ticker, dates, closes, splits, dividends):
    """Raw rows plus CRSP-style multiplicative adjusted closes, as WIKI publishes them."""
    n = len(dates)
    factor = np.ones(n)
    for t in range(n - 1, 0, -1):
        step = (1.0 / splits[t]) * (1.0 - dividends[t] / closes[t - 1])
        factor[t - 1] = factor[t] * step
    return _frame([_row(ticker, d, c, dividend=v, split=s, adj=c * f)
                   for d, c, s, v, f in zip(dates, closes, splits, dividends, factor)])


def test_key_is_last_and_redacted():
    url = wiki.build_url({"date.gte": "2011-06-01", "qopts.export": "true"}, "SECRETKEY")
    assert url.startswith("https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES.json?date.gte=2011-06-01")
    assert url.endswith("api_key=SECRETKEY")
    assert "SECRETKEY" not in common.redact(url)


def test_parse_export_status_creating_and_fresh():
    creating = json.dumps({"datatable_bulk_download": {"file": {"link": None, "status": "creating",
                                                                "data_snapshot_time": None},
                                                       "datatable": {"last_refreshed_time": "2018-03-27 23:54:20 UTC"}}})
    status = wiki.parse_export_status(creating.encode())
    assert status == {"status": "creating", "link": None, "snapshot_time": None,
                      "last_refreshed": "2018-03-27 23:54:20 UTC"}
    fresh = json.dumps({"datatable_bulk_download": {"file": {"link": "https://s3/x.zip?sig=1", "status": "fresh",
                                                             "data_snapshot_time": "2018-03-28 01:00:00 UTC"}}})
    assert wiki.parse_export_status(fresh.encode())["link"] == "https://s3/x.zip?sig=1"
    with pytest.raises(RuntimeError):
        wiki.parse_export_status(b'{"quandl_error": {"code": "QEPx04", "message": "bad"}}')


def test_parse_page_reads_rows_and_cursor():
    body = {"datatable": {"columns": [{"name": c, "type": "x"} for c in wiki.COLUMNS],
                          "data": [_row("AAPL", "2018-03-27", 168.34)]},
            "meta": {"next_cursor_id": "abc"}}
    frame, cursor = wiki.parse_page(json.dumps(body).encode())
    assert cursor == "abc"
    assert frame.loc[0, "ticker"] == "AAPL" and frame.loc[0, "close"] == 168.34
    body["meta"]["next_cursor_id"] = None
    assert wiki.parse_page(json.dumps(body).encode())[1] is None


@pytest.mark.parametrize("header", [True, False])
def test_read_export_zip_filters_dates_and_keeps_all_columns(tmp_path, header):
    lines = [",".join(map(str, _row("A", "2011-05-31", 10.0))),
             ",".join(map(str, _row("A", "2011-06-01", 11.0, dividend=0.25))),
             ",".join(map(str, _row("BRK_A", "2015-01-02", 200000.0, split=2.0)))]
    text = "\n".join(([HEADER] if header else []) + lines) + "\n"
    path = tmp_path / "WIKI_PRICES_x.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("WIKI_PRICES_212b.csv", text)
    frame = wiki.read_export_zip(path)
    assert list(frame.columns) == wiki.COLUMNS
    assert frame["date"].tolist() == ["2011-06-01", "2015-01-02"]
    assert frame["ex-dividend"].tolist() == [0.25, 0.0]
    assert frame["split_ratio"].tolist() == [1.0, 2.0]
    assert frame["ticker"].tolist() == ["A", "BRK_A"]


def test_acquire_uses_a_cached_zip_without_requests(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "wiki"
    raw.mkdir(parents=True)
    with zipfile.ZipFile(raw / "WIKI_PRICES_20261001T000000Z.zip", "w") as archive:
        archive.writestr("w.csv", HEADER + "\n" + ",".join(map(str, _row("A", "2012-01-03", 5.0))) + "\n")
    monkeypatch.setattr(wiki, "RAW_WIKI", raw)
    monkeypatch.setattr(wiki, "cached_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    monkeypatch.setattr(wiki, "_api_key", lambda: (_ for _ in ()).throw(AssertionError("no key read")))
    frame, origin = wiki.acquire(max_wait_s=0)
    assert origin["method"] == "export_zip" and len(frame) == 1


def test_adjustment_factor_moves_only_on_events():
    dates = ["2014-06-04", "2014-06-05", "2014-06-06", "2014-06-09", "2014-06-10"]
    closes = [644.0, 647.0, 646.0, 93.7, 94.25]
    frame = _adjusted_series("AAPL", dates, closes, [1, 1, 1, 7.0, 1], [0, 0.47, 0, 0, 0])
    assert not wiki.adjustment_factor_breaks(frame).any()
    broken = frame.copy()
    broken.loc[1, "adj_close"] *= 1.01  # factor jumps on a day with no event after it
    flagged = wiki.adjustment_factor_breaks(broken)
    assert flagged.tolist() == [False, True, True, False, False]


def test_flat_runs():
    close = pd.Series([1.0, 2.0, 2.0, 2.0, 3.0, 3.0, np.nan, np.nan, np.nan, 4.0, 4.0, 4.0, 4.0])
    assert wiki.flat_runs(close) == [(1, 3), (9, 4)]


def test_check_ticker_flags_single_stock_data_errors():
    sessions = pd.DatetimeIndex(pd.bdate_range("2012-01-02", "2012-03-30"))
    dates = [d.strftime("%Y-%m-%d") for d in sessions]
    keep = dates[:5] + dates[20:]  # 15-session gap
    closes = np.linspace(20, 30, len(keep))
    rows = [_row("X", d, float(c)) for d, c in zip(keep, closes)]
    rows[2] = _row("X", keep[2], 50.0)                       # ratio > 2 with no split
    rows[3] = _row("X", keep[3], 25.0, split=2.0)            # ratio < 0.5 is a split: not flagged
    rows[4][6] = 0.0                                          # zero volume
    rows[6][3] = rows[6][5] - 1.0                             # high below close
    rows.append(_row("X", "2012-01-01", 20.0))               # a Sunday
    group = _frame(rows).sort_values("date").reset_index(drop=True)
    summary, flags, splits, dividends, missing = wiki.check_ticker(group, sessions)
    rules = pd.DataFrame(flags).groupby("rule")["date"].apply(list).to_dict()
    assert rules["raw_close_ratio_no_split"] == [keep[2]]
    assert rules["zero_volume"] == [keep[4]]
    assert rules["ohlc_inconsistent"] == [keep[6]]
    assert rules["not_an_xnas_session"] == ["2012-01-01"]
    assert rules["gap_gt_10_sessions"] == [keep[5]]
    assert summary["max_gap_sessions"] == 15 and summary["missing_sessions"] == 15
    assert summary["splits"] == 1 and splits[0]["split_ratio"] == 2.0
    assert summary["dividends"] == 0 and dividends == []
    assert summary["rows"] == len(rows) and summary["first_date"] == "2012-01-01"
    assert [m["date"] for m in missing] == dates[5:20]
    assert {(m["run_start"], m["run_end"], m["run_sessions"]) for m in missing} == {(dates[5], dates[19], 15)}
    assert summary["missing_session_runs"] == 1
    assert splits[0]["prev_source"] == "same_ticker" and splits[0]["close_prev_raw"] == 50.0


def test_basis_ratios_are_flat_when_bases_match():
    dates = ["2013-01-02", "2013-01-03", "2013-01-04", "2013-01-07", "2013-01-08", "2013-01-09"]
    closes = [100.0, 102.0, 51.5, 52.0, 51.0, 52.5]
    splits = [1, 1, 2.0, 1, 1, 1]
    dividends = [0, 0, 0, 0, 0.8, 0]
    wiki_rows = _adjusted_series("Z", dates, closes, splits, dividends)
    # a stored file adjusted the same way, then divided by 4 for a later split and scaled for later dividends
    stored = pd.DataFrame({"date": dates, "close": wiki_rows["adj_close"] / 4.0 * 0.97})
    daily = wiki.basis_ratios(wiki_rows, stored)
    assert np.allclose(daily["ratio_full"], 4.0 / 0.97)
    assert np.allclose(daily["ratio_adj"], 4.0 / 0.97)
    assert np.allclose(daily["wiki_internal"], 1.0)
    assert np.isclose(daily["s_after"].iloc[0], 2.0) and daily["s_after"].iloc[2] == 1.0
    # removing only the split basis leaves the dividend drift
    assert daily["ratio_split_only"].iloc[0] > daily["ratio_split_only"].iloc[-1]
    summary = wiki.summarise_ratios(daily)
    assert summary["days_dev_gt_0p5pct"] == 0 and summary["overlap_days"] == 6


def test_safe_ticker():
    assert wiki.safe_ticker("BRK_A") == "BRK_A"
    assert wiki.safe_ticker("A/B") == "A_B"


def test_build_outputs_writes_per_ticker_files(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "OUT", tmp_path)
    monkeypatch.setattr(wiki, "BY_TICKER", tmp_path / "by_ticker")
    sessions = pd.bdate_range("2018-03-19", "2018-03-27")
    monkeypatch.setattr(wiki, "xnas_sessions", lambda *a, **k: sessions)
    rows = [_row(t, d.strftime("%Y-%m-%d"), 10.0 + i) for t in ("B", "A") for i, d in enumerate(sessions)]
    (tmp_path / "by_ticker").mkdir()
    (tmp_path / "by_ticker" / "OLD.csv.gz").write_bytes(b"")
    facts = wiki.build_outputs(_frame(rows), {"method": "test"})
    assert facts["tickers"] == 2 and facts["rows"] == 14 and facts["last_date"] == "2018-03-27"
    assert sorted(p.name for p in (tmp_path / "by_ticker").iterdir()) == ["A.csv.gz", "B.csv.gz"]
    back = pd.read_csv(tmp_path / "by_ticker" / "A.csv.gz")
    assert list(back.columns) == wiki.COLUMNS and back["date"].is_monotonic_increasing
    assert gzip.decompress((tmp_path / "by_ticker" / "A.csv.gz").read_bytes()).startswith(HEADER.encode())


def test_cent_rounded_adjusted_prices_are_not_breaks():
    dates = [f"2011-10-{d:02d}" for d in (17, 18, 19, 20, 21, 24)]
    closes = [2.26, 2.33, 2.24, 2.28, 2.58, 2.56]
    rows = [_row("GERN", d, c, adj=round(c * 0.9461, 2)) for d, c in zip(dates, closes)]
    assert not wiki.adjustment_factor_breaks(_frame(rows)).any()
    rows[3] = _row("GERN", dates[3], 2.28, adj=round(2.28 * 0.9461 * 1.05, 2))  # a 5% jump in the factor
    assert wiki.adjustment_factor_breaks(_frame(rows)).tolist() == [False, False, False, True, True, False]


def test_summarise_ratios_reports_year_medians():
    daily = pd.DataFrame({"date": ["2016-12-30", "2017-01-03", "2017-01-04"], "ratio_full": [2.0, 2.0, 2.02],
                          "ratio_split_only": [2.1, 2.1, 2.1], "ratio_adj": [2.0, 2.0, 2.0],
                          "wiki_internal": [1.0, 1.0, 1.0]})
    summary = wiki.summarise_ratios(daily)
    assert summary["full_median_by_year"] == "2016:2.00000 2017:2.01000"
    assert summary["days_dev_gt_0p5pct"] == 1 and summary["worst_dates"].startswith("2017-01-04:+0.0100")


def _sessions(start, end):
    return pd.DatetimeIndex(pd.bdate_range(start, end))


def test_null_open_high_low_are_counted_and_flagged():
    days = [d.strftime("%Y-%m-%d") for d in _sessions("2014-04-28", "2014-05-02")]
    rows = [_row("ATMI", d, 20.0 + i) for i, d in enumerate(days)]
    rows[1][2] = np.nan                      # open missing
    rows[3][3] = rows[3][4] = np.nan         # high and low missing
    group = _frame(rows)
    summary, flags, *_ = wiki.check_ticker(group, _sessions("2014-04-28", "2014-05-02"))
    assert (summary["null_open"], summary["null_high"], summary["null_low"]) == (1, 1, 1)
    null_flags = [f for f in flags if f["rule"] == "null_ohlc"]
    assert [f["date"] for f in null_flags] == [days[1], days[3]]
    assert null_flags[0]["detail"].startswith("missing=open") and "missing=high,low" in null_flags[1]["detail"]
    assert not any(f["rule"] == "ohlc_inconsistent" for f in flags)


def test_short_session_gaps_are_listed_one_row_per_session():
    sessions = _sessions("2017-07-31", "2017-08-18")
    days = [d.strftime("%Y-%m-%d") for d in sessions]
    keep = [d for i, d in enumerate(days) if i not in (5, 9, 10)]   # one 1-session and one 2-session gap
    group = _frame([_row("AAPL", d, 150.0) for d in keep])
    summary, flags, _, _, missing = wiki.check_ticker(group, sessions)
    assert [(m["date"], m["run_sessions"]) for m in missing] == [(days[5], 1), (days[9], 2), (days[10], 2)]
    assert missing[1]["run_start"] == days[9] and missing[1]["run_end"] == days[10]
    assert summary["missing_sessions"] == 3 and summary["missing_session_runs"] == 2
    assert not any(f["rule"] == "gap_gt_10_sessions" for f in flags)


def test_prior_close_fills_a_first_row_dividend_and_split():
    sessions = _sessions("2011-06-01", "2011-06-07")
    days = [d.strftime("%Y-%m-%d") for d in sessions]
    rows = [_row("BAC", d, 11.0) for d in days]
    rows[0] = _row("BAC", days[0], 11.0, dividend=0.01, split=2.0)
    group = _frame(rows)
    _, _, splits, dividends, _ = wiki.check_ticker(group, sessions, {"date": "2011-05-31", "close": 10.0})
    assert dividends[0]["pct_of_prior_close"] == pytest.approx(0.001)
    assert (dividends[0]["prev_date"], dividends[0]["prev_source"]) == ("2011-05-31", "prior_request")
    assert splits[0]["close_prev_raw"] == 10.0
    _, _, _, without, _ = wiki.check_ticker(group, sessions, None)
    assert np.isnan(without[0]["pct_of_prior_close"]) and without[0]["prev_source"] == "no_prior_row"
    later = _frame([_row("KHC", d, 70.0, dividend=16.5 if i == 0 else 0.0) for i, d in enumerate(days[2:])])
    _, _, _, first, _ = wiki.check_ticker(later, sessions, {"date": "2011-05-31", "close": 10.0})
    assert first[0]["prev_source"] == "none_first_row" and np.isnan(first[0]["pct_of_prior_close"])


def _page(rows, cursor=None):
    return json.dumps({"datatable": {"columns": [{"name": c} for c in wiki.COLUMNS], "data": rows},
                       "meta": {"next_cursor_id": cursor}}).encode()


def test_fetch_paged_keys_pages_by_cursor_and_restarts_an_expired_chain(tmp_path, monkeypatch):
    cache = tmp_path / "pages"
    cache.mkdir()
    # a chain cached by an earlier run: its second page points to a cursor that has since expired
    (cache / "00000_first.json.gz").write_bytes(gzip.compress(_page([_row("A", "2012-01-03", 1.0)], "old1")))
    (cache / f"00001_{wiki._cursor_tag('old1')}.json.gz").write_bytes(
        gzip.compress(_page([_row("A", "2012-01-04", 1.0)], "old2")))
    fresh = {None: _page([_row("A", "2012-01-03", 1.0)], "new1"), "new1": _page([_row("B", "2012-01-03", 2.0)])}
    asked = []

    def fake_get(params, path, symbol=""):
        path = Path(path)
        if path.exists():
            return gzip.decompress(path.read_bytes())
        cursor = params.get("qopts.cursor_id")
        asked.append(cursor)
        if cursor not in fresh:
            raise RuntimeError("cursor expired")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(fresh[cursor]))
        return fresh[cursor]

    monkeypatch.setattr(wiki, "api_get", fake_get)
    paths = wiki.fetch_paged({"date.gte": "2011-06-01"}, cache)
    assert asked == ["old2", None, "new1"]
    assert [p.name for p in paths] == ["00000_first.json.gz", f"00001_{wiki._cursor_tag('new1')}.json.gz"]
    assert wiki.read_pages(paths)["ticker"].tolist() == ["A", "B"]
    stale = [p for p in tmp_path.iterdir() if p.name.startswith("pages_stale_")]
    assert len(stale) == 1 and len(list(stale[0].glob("*.json.gz"))) == 2
    # a second run is served wholly from the cache
    asked.clear()
    assert wiki.fetch_paged({"date.gte": "2011-06-01"}, cache) == paths and asked == []


def test_fetch_prior_rows_uses_the_previous_session_then_a_window(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "RAW_WIKI", tmp_path)
    seen = []

    def fake_get(params, path, symbol=""):
        seen.append(dict(params))
        if params.get("date") == "2011-05-31":
            body = _page([_row("A", "2011-05-31", 10.0), _row("B", "2011-05-31", 20.0)])
        else:
            assert params["date.gte"] == "2011-05-18" and params["date.lt"] == "2011-06-01"
            assert params["ticker"] == "C,D"
            body = _page([_row("C", "2011-05-26", 30.0), _row("C", "2011-05-27", 31.0)])
        common.atomic_write(path, gzip.compress(body))
        return body

    monkeypatch.setattr(wiki, "api_get", fake_get)
    prior, facts = wiki.fetch_prior_rows(["A", "C", "D"])
    assert prior[["ticker", "date", "close"]].values.tolist() == [
        ["A", "2011-05-31", 10.0], ["B", "2011-05-31", 20.0], ["C", "2011-05-27", 31.0]]
    assert facts["session"] == "2011-05-31" and facts["tickers_needing_window"] == 2
    assert facts["tickers_asked_without_prior_row"] == 1 and len(seen) == 2


def test_tickers_read_back_as_text(tmp_path):
    path = tmp_path / "TRUE.csv.gz"
    path.write_bytes(wiki._csv_gz_bytes(_frame([_row("TRUE", "2014-05-16", 9.0), _row("TRUE", "2014-05-19", 9.5)])))
    assert pd.read_csv(path)["ticker"].dtype == bool          # the pitfall
    back = wiki.read_ticker_file(path)
    assert back["ticker"].tolist() == ["TRUE", "TRUE"] and back["date"].tolist() == ["2014-05-16", "2014-05-19"]
    assert wiki.misread_by_default(["AAPL", "TRUE", "NA", "NULL", "BRK_A"]) == ["TRUE", "NA", "NULL"]


def test_file_name_collisions_are_checked_without_case(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "OUT", tmp_path)
    monkeypatch.setattr(wiki, "BY_TICKER", tmp_path / "by_ticker")
    sessions = pd.bdate_range("2018-03-19", "2018-03-21")
    monkeypatch.setattr(wiki, "xnas_sessions", lambda *a, **k: sessions)
    rows = [_row(t, d.strftime("%Y-%m-%d"), 10.0) for t in ("AB", "Ab") for d in sessions]
    with pytest.raises(ValueError, match="map to the file name"):
        wiki.build_outputs(_frame(rows), {"method": "test"})


def test_stale_cleanup_never_deletes_a_case_variant_of_a_current_file(tmp_path):
    (tmp_path / "abc.csv.gz").write_bytes(b"old")
    (tmp_path / "OLD.csv.gz").write_bytes(b"old")
    common.atomic_write(tmp_path / "ABC.csv.gz", b"new")
    result = wiki._remove_stale(tmp_path, {"ABC.csv.gz"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ABC.csv.gz"]
    assert (tmp_path / "ABC.csv.gz").read_bytes() == b"new" and "OLD.csv.gz" in result["removed"]


def test_probes_are_reproduced_from_their_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "RAW_WIKI", tmp_path)
    monkeypatch.setattr(wiki, "DELISTED_PROBES", ["BMC", "DELL"])
    monkeypatch.setattr(wiki, "cached_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    monkeypatch.setattr(wiki, "_api_key", lambda: (_ for _ in ()).throw(AssertionError("no key read")))
    (tmp_path / "probe").mkdir()
    aapl = [_row("AAPL", "2018-03-27", 168.34), _row("AAPL", "2018-03-26", 172.77)]
    (tmp_path / "probe" / wiki.KEY_PROBE_FILE).write_bytes(_page(aapl))
    for ticker, rows in {"BMC": [_row("BMC", "2010-12-31", 47.0)],
                         "DELL": [_row("DELL", "2013-10-29", 13.86), _row("DELL", "2013-10-30", 13.86, volume=0.0)]
                         }.items():
        (tmp_path / "probe" / f"{ticker}_all").mkdir()
        (tmp_path / "probe" / f"{ticker}_all" / "00000_first.json.gz").write_bytes(gzip.compress(_page(rows)))
    export = _frame([_row("AAPL", "2018-03-26", 172.77), _row("AAPL", "2018-03-27", 168.34),
                     _row("DELL", "2013-10-29", 13.86)])
    probes = wiki.run_probes(export)
    assert probes["key_probe"]["rows"] == 2 and probes["key_probe"]["rows_in_export"] == 2
    assert probes["key_probe"]["max_abs_diff_vs_export"] == 0.0
    assert probes["delisted"]["BMC"]["rows_on_or_after_start"] == 0
    assert probes["delisted"]["BMC"]["last_date"] == "2010-12-31"
    dell = probes["delisted"]["DELL"]
    assert dell["last_traded_date"] == "2013-10-29" and dell["rows_missing_from_export"] == 1


def test_survivorship_counts_form25_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "OUT", tmp_path)
    form25 = pd.DataFrame({
        "accession": ["a1", "a2", "a3", "a4", "a5"],
        "effective_date": ["2012-05-10", "2013-11-08", "2013-06-01", "2015-03-02", "2016-01-04"],
        "subject_name": ["Gone Co", "Dell Inc", "Unit Trust", "Later Co", "Reuse Co"],
        "classification": ["common_delisting", "common_delisting", "other_class", "common_delisting",
                           "common_delisting"],
        "class_kind": ["common", "common", "unit", "common", "common"],
        "subject_tickers_sec": ["", "", "", "", ""],
    })
    master = pd.DataFrame({"delist_form25_accession": ["a1", "a2", "a4", "a5", ""],
                           "tickers_observed": ["GONE", "DELL", "LATR", "REUS", "AAPL"],
                           "foreign_filer": ["N", "N", "N", "Y", "N"]})
    form25_path, master_path = tmp_path / "f25.csv", tmp_path / "sm.csv"
    form25.to_csv(form25_path, index=False)
    master.to_csv(master_path, index=False)
    frame = _frame([_row("DELL", "2013-10-29", 13.86), _row("DELL", "2014-04-01", 13.86, volume=0.0),
                    _row("LATR", "2015-02-27", 5.0), _row("REUS", "2017-01-03", 3.0),
                    _row("AAPL", "2018-03-27", 168.0)])
    summary = pd.DataFrame({"ticker": ["AAPL", "DELL", "LATR", "REUS"],
                            "first_date": ["2018-03-27", "2013-10-29", "2015-02-27", "2017-01-03"],
                            "last_date": ["2018-03-27", "2014-04-01", "2015-02-27", "2017-01-03"],
                            "last_traded_date": ["2018-03-27", "2013-10-29", "2015-02-27", "2017-01-03"],
                            "rows_after_last_traded": [0, 1, 0, 0]})
    probes = {"delisted": {"BMC": {"rows_any_date": 0, "rows_on_or_after_start": 0}}}
    prior = _frame([_row("DELL", "2011-05-31", 16.0), _row("GONE", "2011-05-31", 3.0)])
    out = wiki.survivorship(frame, summary, probes, prior, form25_path, master_path)
    first, after = out["form25"]["periods"]["2012-01..2014-03"], out["form25"]["periods"]["2014-04..2018-03"]
    assert (first["form25_common_delistings"], first["ticker_in_wiki"], first["covered"]) == (2, 1, 1)
    assert (after["form25_common_delistings"], after["covered"], after["covered_domestic"]) == (2, 1, 1)
    assert after["wiki_rows_on_or_before_effective"] == 1          # REUS rows only after its delisting
    assert out["tickers_last_date_before_cutoff"] == 0 and out["min_last_date"] == "2014-04-01"
    assert [t["ticker"] for t in out["tickers_last_traded_before_cutoff"]] == ["DELL"]
    assert out["form25"]["covered_2012_01_to_2014_03"][0]["covering_tickers"] == "DELL"
    assert "BMC" in out["note"] and "survivor bias" in out["note"]
    assert out["prior_session_check"]["tickers_not_in_export"] == 1 and out["prior_session_check"]["examples"] == ["GONE"]
    assert (first["covered_last_trading_days"], first["covered_ticker_trades_on"]) == (1, 0)
    assert "last trading days of only 1 (DELL)" in out["note"]
    table = wiki.read_ticker_file(tmp_path / "wiki_form25_coverage.csv")
    assert table["covered"].tolist() == [False, True, True, False]
    assert table["trades_on"].tolist() == [False, False, False, False]


def test_ratio_steps_find_level_shifts_not_one_day_spikes():
    values = [1.06672] * 20 + [1.06408] * 20
    values[8] *= 1.012                                   # a one-day error in the stored file
    daily = pd.DataFrame({"date": [f"d{i:02d}" for i in range(40)], "ratio_full": values})
    steps = wiki.ratio_steps(daily)
    assert len(steps) == 1 and steps[0][0] == "d20" and steps[0][1] == pytest.approx(1.06408 / 1.06672 - 1)


def test_previous_session_and_wiki_symbols():
    assert wiki.previous_session("2011-06-01") == "2011-05-31"
    assert wiki.wiki_symbol("brk.b") == "BRK_B" and wiki.wiki_symbol("BF-A") == "BF_A"


def test_request_export_strips_the_presigned_query_from_the_cached_status(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki, "RAW_WIKI", tmp_path)
    link = "https://bucket.s3.amazonaws.com/export/x.zip?X-Amz-Credential=AKIA1&X-Amz-Signature=abc"
    payload = json.dumps({"datatable_bulk_download": {"file": {"link": link, "status": "fresh",
                                                                "data_snapshot_time": "t"}}}).encode()

    def fake_get(url, cache_path, **kwargs):
        common.atomic_write(cache_path, payload)
        return payload

    monkeypatch.setattr(wiki, "cached_get", fake_get)
    status = wiki.request_export("KEY", filtered=True, max_wait_s=0)
    assert status["link"] == link
    text = Path(status["status_file"]).read_text()
    assert "AKIA1" not in text and "abc" not in text and "x.zip?REDACTED" in text


def test_form25_coverage_separates_a_ticker_that_trades_on():
    form25 = pd.DataFrame({"accession": ["r1", "r2"], "effective_date": ["2013-06-17", "2013-06-17"],
                           "subject_name": ["Liberty Global, Inc.", "Old Holdco"],
                           "classification": ["common_delisting"] * 2, "class_kind": ["common"] * 2,
                           "subject_tickers_sec": ["LBTYA", "brk.b"]})
    frame = _frame([_row("LBTYA", "2013-06-14", 70.0), _row("LBTYA", "2013-09-03", 72.0),
                    _row("BRK_B", "2013-06-14", 110.0)])
    table = wiki.form25_coverage(form25, None, frame, {"LBTYA": "2011-06-01", "BRK_B": "2011-06-01"})
    assert table["covered"].tolist() == [True, True]
    assert table["trades_on"].tolist() == [True, False]
    assert table["covering_tickers"].tolist() == ["LBTYA", "BRK_B"]
    counts = wiki._coverage_counts(table.assign(foreign_filer="N"))
    assert (counts["covered_ticker_trades_on"], counts["covered_last_trading_days"]) == (1, 1)
