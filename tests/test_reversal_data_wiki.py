import gzip
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
    summary, flags, splits, dividends = wiki.check_ticker(group, sessions)
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
