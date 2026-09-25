from datetime import date
import json

import pandas as pd

from src.io import nasdaq_update


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_refresh_universe_can_persist_only_investable_common_equities(
    tmp_path, monkeypatch
):
    template = tmp_path / "template.csv"
    target = tmp_path / "research" / "current_universe.csv"
    pd.DataFrame({
        "Symbol": ["OLD"],
        "Name": ["Old Company Common Stock"],
    }).to_csv(template, index=False)
    payload = {"data": {"table": {"rows": [
        {"symbol": "LIVE", "name": "Live Company Common Stock", "marketCap": "100"},
        {"symbol": "LIVEW", "name": "Live Company Warrant", "marketCap": "20"},
        {"symbol": None, "name": "Malformed row", "marketCap": "10"},
    ]}}}
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", str(template))
    monkeypatch.setattr(
        nasdaq_update, "urlopen", lambda *_args, **_kwargs: _JsonResponse(payload)
    )

    result = nasdaq_update.refresh_universe(
        date(2026, 8, 10), min_market_cap=0, target_path=target,
        common_equities_only=True,
    )

    assert pd.read_csv(target)["Symbol"].tolist() == ["LIVE"]
    assert pd.read_csv(result["snapshot"])["Symbol"].tolist() == ["LIVE"]
    assert result["unfiltered_count"] == 2
    assert result["excluded_non_common_securities"] == 1
    assert result["common_equities_only"] is True


def test_closed_index_snapshot_requires_matching_closed_official_session(
    monkeypatch,
):
    chart = {"data": {
        "timeAsOf": "Aug 10, 2026",
        "lastSalePrice": "26,605.36",
    }}
    info = {"data": {
        "marketStatus": "Closed",
        "primaryData": {"lastTradeTimestamp": "Aug 10, 2026"},
    }}

    def fake_urlopen(request, **_kwargs):
        payload = chart if "/chart" in request.full_url else info
        return _JsonResponse(payload)

    monkeypatch.setattr(nasdaq_update, "urlopen", fake_urlopen)

    result = nasdaq_update.fetch_closed_index_snapshot(
        "COMP", date(2026, 8, 10)
    )

    assert result["date"] == "2026-08-10"
    assert result["close"] == 26605.36
    assert result["market_status"] == "Closed"
    assert len(result["payload_sha256"]) == 64


def _configure_partial_update(tmp_path, monkeypatch):
    universe = tmp_path / "universe.csv"
    index = tmp_path / "index.csv"
    price_dir = tmp_path / "prices"
    pd.DataFrame({
        "Symbol": ["ABC", "DEF"],
        "Name": ["ABC Common Stock", "DEF Common Stock"],
    }).to_csv(universe, index=False)
    pd.DataFrame({
        "date": ["2026-07-29"],
        "close": [100],
    }).to_csv(index, index=False)
    monkeypatch.setattr(
        nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", str(universe)
    )
    monkeypatch.setattr(nasdaq_update, "NASDAQ_INDEX_FILE", str(index))
    monkeypatch.setattr(nasdaq_update, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(
        nasdaq_update,
        "refresh_universe",
        lambda _end: (_ for _ in ()).throw(
            AssertionError("partial update refreshed formal universe")
        ),
    )
    return universe


def test_targeted_update_requests_only_explicit_tickers(
    tmp_path, monkeypatch
):
    universe = _configure_partial_update(tmp_path, monkeypatch)
    requested = []

    def fake_update(ticker, _end, _price_dir, _known=None):
        requested.append(ticker)
        return {"ticker": ticker, "status": "current", "rows": 0}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    before = universe.read_bytes()

    result = nasdaq_update.update_all(
        date(2026, 7, 29),
        workers=1,
        tickers=["metcb", "METCB"],
    )

    assert requested == ["METCB"]
    assert result["requested_ticker_count"] == 1
    assert result["requested_tickers"] == ["METCB"]
    assert result["counts"] == {"current": 1}
    assert result["universe"]["mode"] == "retained_for_partial_update"
    assert result["universe"]["snapshot"] is None
    assert universe.read_bytes() == before


def test_limited_update_does_not_advance_formal_universe(
    tmp_path, monkeypatch
):
    universe = _configure_partial_update(tmp_path, monkeypatch)
    requested = []

    def fake_update(ticker, _end, _price_dir, _known=None):
        requested.append(ticker)
        return {"ticker": ticker, "status": "current", "rows": 0}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    before = universe.read_bytes()

    result = nasdaq_update.update_all(
        date(2026, 7, 29),
        workers=1,
        limit=1,
    )

    assert requested == ["ABC"]
    assert result["requested_ticker_count"] == 1
    assert result["requested_tickers"] is None
    assert result["universe"]["mode"] == "retained_for_partial_update"
    assert universe.read_bytes() == before


def test_full_update_retries_only_failed_tickers_at_lower_concurrency(
    tmp_path, monkeypatch
):
    universe = _configure_partial_update(tmp_path, monkeypatch)
    attempts = {"ABC": 0, "DEF": 0}

    def fake_update(ticker, _end, _price_dir, _known=None):
        attempts[ticker] += 1
        if ticker == "DEF" and attempts[ticker] == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        return {"ticker": ticker, "status": "current", "rows": 0}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    monkeypatch.setattr(nasdaq_update.time, "sleep", lambda _seconds: None)

    result = nasdaq_update.update_all(
        date(2026, 7, 29), workers=8, tickers=["ABC", "DEF"],
    )

    assert attempts == {"ABC": 1, "DEF": 2}
    assert result["counts"] == {"current": 2}
    assert result["failures"] == []


def test_history_requests_bypass_nasdaqs_per_query_cache(monkeypatch) -> None:
    urls = []
    payload = {"data": {"tradesTable": {"rows": [
        {"date": "09/23/2026", "open": "1", "high": "1", "low": "1",
         "close": "26,936.04", "volume": "--"},
    ]}}}

    def fake_urlopen(request, **_kwargs):
        urls.append(request.full_url)
        if len(urls) == 1:
            raise OSError("transient")
        return _JsonResponse(payload)

    monkeypatch.setattr(nasdaq_update, "urlopen", fake_urlopen)
    monkeypatch.setattr(nasdaq_update.time, "sleep", lambda _seconds: None)
    frame = nasdaq_update.fetch_history(
        "COMP", date(2026, 9, 13), date(2026, 9, 23), asset_class="index"
    )

    assert frame["close"].tolist() == [26936.04]
    # Each attempt, including the retry, is a distinct query.
    assert len(urls) == 2 and urls[0] != urls[1]
    for url in urls:
        assert "fromdate=2026-06-25&todate=2026-09-23" in url
        assert "&_=" in url
    assert nasdaq_update.uncached("https://x.test/a").startswith("https://x.test/a?_=")


def test_short_history_requests_are_widened_and_trimmed(monkeypatch) -> None:
    """A one-session range comes back empty from Nasdaq, so ask for 90 days."""
    urls = []
    payload = {"data": {"tradesTable": {"rows": [
        {"date": f"09/{day:02d}/2026", "open": "1", "high": "1", "low": "1",
         "close": str(day), "volume": "10"}
        for day in (24, 23, 22)
    ]}}}

    def fake_urlopen(request, **_kwargs):
        urls.append(request.full_url)
        return _JsonResponse(payload)

    monkeypatch.setattr(nasdaq_update, "urlopen", fake_urlopen)
    frame = nasdaq_update.fetch_history("AAPL", date(2026, 9, 24), date(2026, 9, 24))
    assert "fromdate=2026-06-26&todate=2026-09-24" in urls[-1]
    assert frame["date"].tolist() == [pd.Timestamp("2026-09-24")]
    assert frame["close"].tolist() == [24.0]
    assert frame.index.tolist() == [0]

    # A range longer than the minimum is requested as given.
    nasdaq_update.fetch_history("AAPL", date(2020, 1, 1), date(2026, 9, 24))
    assert "fromdate=2020-01-01&todate=2026-09-24" in urls[-1]


def _sessions(start, periods):
    return pd.bdate_range(start, periods=periods)


def _stored(dates, closes):
    return pd.DataFrame({"date": dates, "close": closes})


def test_reconcile_finds_a_provider_rescaling_of_the_whole_overlap() -> None:
    dates = _sessions("2026-07-01", 12)
    stored = _stored(dates, [100.0 + day for day in range(12)])
    fetched = _stored(dates, [(100.0 + day) / 2 for day in range(12)])

    result = nasdaq_update.reconcile_provider_history(stored, fetched)

    assert result["status"] == "ADJUSTED"
    assert result["factor"] == 0.5
    assert result["boundary"] is None
    assert result["rescaled_sessions"] == 12


def test_reconcile_places_a_rescaling_inside_the_overlap_on_its_step() -> None:
    """Rows appended after an unreconciled split are already in new units."""
    dates = _sessions("2026-07-01", 10)
    closes = [90.0, 91, 92, 93, 94, 95, 32, 33, 34, 35]
    stored = _stored(dates, closes)
    fetched = _stored(dates, [value / 3 for value in closes[:6]] + closes[6:])

    result = nasdaq_update.reconcile_provider_history(stored, fetched)

    assert result["status"] == "ADJUSTED"
    assert abs(result["factor"] - 1 / 3) < 1e-12
    assert result["boundary"] == f"{dates[6]:%Y-%m-%d}"


def test_reconcile_accepts_matching_history_and_isolated_corrections() -> None:
    dates = _sessions("2026-07-01", 12)
    stored = _stored(dates, [50.0 + day for day in range(12)])
    assert nasdaq_update.reconcile_provider_history(stored, stored.copy())[
        "status"
    ] == "CONSISTENT"

    corrected = stored.copy()
    corrected.loc[2, "close"] *= 1.03
    result = nasdaq_update.reconcile_provider_history(stored, corrected)
    assert result["status"] == "CONSISTENT_WITH_REVISIONS"
    assert result["revised_sessions"] == 1


def test_reconcile_applies_recorded_rescalings_before_comparing() -> None:
    dates = _sessions("2026-07-01", 12)
    stored = _stored(dates, [100.0 + day for day in range(12)])
    fetched = _stored(dates, [(100.0 + day) / 2 for day in range(12)])
    known = pd.DataFrame({
        "ticker": ["ABC"],
        "split_date": [pd.Timestamp("2026-07-20")],
        "confirmed_adjustment_factor": [0.5],
    })

    assert nasdaq_update.reconcile_provider_history(stored, fetched, known)[
        "status"
    ] == "CONSISTENT"


def test_reconcile_refuses_histories_that_do_not_match_consistently() -> None:
    dates = _sessions("2026-07-01", 12)
    stored = _stored(dates, [100.0] * 12)
    scattered = _stored(dates, [100.0 * (1 + 0.07 * ((-1) ** day)) for day in range(12)])
    assert nasdaq_update.reconcile_provider_history(stored, scattered)[
        "status"
    ] == "MISMATCH"

    # A small uniform rescaling is not a corporate action.
    slightly = _stored(dates, [99.0] * 12)
    assert nasdaq_update.reconcile_provider_history(stored, slightly)[
        "status"
    ] == "MISMATCH"
    assert nasdaq_update.reconcile_provider_history(stored, stored.iloc[:0])[
        "status"
    ] == "NO_OVERLAP"


def _write_prices(path, dates, closes):
    pd.DataFrame({
        "date": [f"{day:%Y-%m-%d}" for day in dates],
        "ticker": "ABC",
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1000.0,
    }).to_csv(path, index=False)


def test_update_ticker_records_a_split_without_rewriting_stored_rows(
    tmp_path, monkeypatch
) -> None:
    stored_dates = _sessions("2026-06-15", 25)
    new_dates = _sessions(stored_dates[-1] + pd.Timedelta(days=1), 3)
    path = tmp_path / "abc.csv"
    _write_prices(path, stored_dates, [90.0] * 25)
    before = path.read_text()
    requested = []

    def fake_history(symbol, start, end, **_kwargs):
        requested.append((start, end))
        dates = [day for day in [*stored_dates, *new_dates] if day.date() >= start]
        closes = [60.0 if day in stored_dates else 61.0 for day in dates]
        return pd.DataFrame({
            "date": dates, "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": 1500.0,
        })

    monkeypatch.setattr(nasdaq_update, "fetch_history", fake_history)
    result = nasdaq_update.update_ticker("ABC", new_dates[-1].date(), tmp_path)

    # The stored overlap was requested again.
    assert requested[0][0] == (
        stored_dates[-1] + pd.Timedelta(days=1 - nasdaq_update.RECONCILE_OVERLAP_DAYS)
    ).date()
    assert result["status"] == "updated" and result["rows"] == 3
    adjustment = result["provider_adjustment"]
    assert adjustment["split_date"] == f"{new_dates[0]:%Y-%m-%d}"
    assert abs(adjustment["confirmed_adjustment_factor"] - 2 / 3) < 1e-12
    assert adjustment["confirmed_action_type"] == "PROVIDER_ADJUSTMENT_DISCONTINUITY"
    assert abs(adjustment["raw_price_ratio"] - 61.0 / 90.0) < 1e-12
    stored = pd.read_csv(path)
    assert path.read_text().startswith(before.rstrip("\n").split("\n")[0])
    assert stored["close"].tolist() == [90.0] * 25 + [61.0] * 3


def test_update_ticker_appends_nothing_when_the_history_no_longer_matches(
    tmp_path, monkeypatch
) -> None:
    stored_dates = _sessions("2026-06-15", 25)
    path = tmp_path / "abc.csv"
    _write_prices(path, stored_dates, [90.0] * 25)
    before = path.read_bytes()

    def fake_history(symbol, start, end, **_kwargs):
        dates = [day for day in stored_dates if day.date() >= start]
        dates.append(stored_dates[-1] + pd.Timedelta(days=3))
        closes = [90.0 * (1 + 0.1 * ((-1) ** i)) for i in range(len(dates))]
        return pd.DataFrame({
            "date": dates, "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": 1000.0,
        })

    monkeypatch.setattr(nasdaq_update, "fetch_history", fake_history)
    try:
        nasdaq_update.update_ticker("ABC", date(2026, 7, 31), tmp_path)
    except RuntimeError as exc:
        assert "no longer matches" in str(exc)
    else:
        raise AssertionError("a mismatched history was appended")
    assert path.read_bytes() == before


def test_update_all_records_each_provider_rescaling_once(tmp_path, monkeypatch) -> None:
    _configure_partial_update(tmp_path, monkeypatch)
    price_dir = tmp_path / "market" / "prices"
    price_dir.mkdir(parents=True)
    index = tmp_path / "market" / "index.csv"
    pd.DataFrame({"date": ["2026-07-29"], "close": [100]}).to_csv(index, index=False)
    row = {
        "ticker": "ABC", "split_date": "2026-07-21", "raw_price_ratio": 0.49,
        "matched_factor": 0.5, "validation_status": "CONFIRMED",
        "confirmed_action_type": "PROVIDER_ADJUSTMENT_DISCONTINUITY",
        "confirmed_action_date": "2026-07-21", "confirmed_adjustment_factor": 0.5,
        "primary_source": "nasdaq_history_overlap", "overlap_sessions": 13,
        "overlap_first": "2026-07-01", "overlap_last": "2026-07-20",
        "ratio_spread": 0.0, "recorded_at": "2026-09-25T00:00:00+00:00",
    }
    seen_known = []

    def fake_update(ticker, _end, _price_dir, known=None):
        seen_known.append(len(known))
        return {"ticker": ticker, "status": "current", "rows": 0,
                "provider_adjustment": dict(row)}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    first = nasdaq_update.update_all(
        date(2026, 7, 29), workers=1, tickers=["ABC"], price_dir=price_dir,
        index_path=index,
    )
    second = nasdaq_update.update_all(
        date(2026, 7, 29), workers=1, tickers=["ABC"], price_dir=price_dir,
        index_path=index,
    )

    path = nasdaq_update.provider_adjustments_path(price_dir)
    assert path == tmp_path / "market" / "provider_adjustments.csv"
    assert len(first["provider_adjustments_recorded"]) == 1
    assert second["provider_adjustments_recorded"] == []
    assert seen_known == [0, 1]
    recorded = nasdaq_update.load_provider_adjustments(path)
    assert recorded["ticker"].tolist() == ["ABC"]
    assert recorded["split_date"].tolist() == [pd.Timestamp("2026-07-21")]
