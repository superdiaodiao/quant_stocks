from pathlib import Path

import pandas as pd
import pytest

from src.research import prospective_marks as marks


def _day(text: str) -> pd.Timestamp:
    return pd.Timestamp(text)


SCHEDULE = pd.DataFrame([
    {"effective_date": "2026-10-01", "ticker": "AAA", "target_weight": 0.2},
    {"effective_date": "2026-10-01", "ticker": "BBB", "target_weight": 0.2},
    {"effective_date": "2026-11-02", "ticker": "AAA", "target_weight": 0.2},
    {"effective_date": "2026-11-02", "ticker": "CCC", "target_weight": 0.2},
    {"effective_date": "2026-12-01", "ticker": "__CASH__", "target_weight": 0.0},
])


def test_holding_windows_merge_continued_positions_and_skip_cash() -> None:
    windows = marks.holding_windows(SCHEDULE, "2026-12-15")
    assert windows == {
        "AAA": [(_day("2026-10-01"), _day("2026-12-01"))],
        "BBB": [(_day("2026-10-01"), _day("2026-11-02"))],
        "CCC": [(_day("2026-11-02"), _day("2026-12-01"))],
    }
    # Targets effective after the valuation date are not held yet.
    assert marks.holding_windows(SCHEDULE, "2026-10-20") == {
        "AAA": [(_day("2026-10-01"), _day("2026-10-20"))],
        "BBB": [(_day("2026-10-01"), _day("2026-10-20"))],
    }


def test_closes_required_are_held_into_or_bought_on_the_date() -> None:
    assert marks.closes_required(SCHEDULE, "2026-10-01") == ["AAA", "BBB"]
    assert marks.closes_required(SCHEDULE, "2026-10-15") == ["AAA", "BBB"]
    # Rebalance day: BBB is sold at this close, CCC bought at it.
    assert marks.closes_required(SCHEDULE, "2026-11-02") == ["AAA", "BBB", "CCC"]
    assert marks.closes_required(SCHEDULE, "2026-11-03") == ["AAA", "CCC"]
    assert marks.closes_required(SCHEDULE, "2026-12-02") == []


def test_unpriced_holdings_flag_only_prices_that_stop_while_held() -> None:
    dates = pd.bdate_range("2026-10-01", "2026-12-15")
    close = pd.DataFrame(1.0, index=dates, columns=["AAA", "BBB", "CCC"])
    close.loc[_day("2026-11-10"):, "BBB"] = None   # delisted after it was sold
    close.loc[_day("2026-11-20"):, "CCC"] = None   # delisted while held
    windows = marks.holding_windows(SCHEDULE, "2026-12-15")
    assert marks.unpriced_holdings(close, windows, "2026-12-15") == [{
        "ticker": "CCC",
        "last_price_date": "2026-11-19",
        "holding_window": ["2026-11-02", "2026-12-01"],
    }]
    # Before the valuation reaches the gap nothing is missing.
    early = marks.holding_windows(SCHEDULE, "2026-11-19")
    assert marks.unpriced_holdings(close, early, "2026-11-19") == []


def test_inside_holdings_masks_events_by_ticker_window() -> None:
    windows = marks.holding_windows(SCHEDULE, "2026-12-15")
    events = pd.DataFrame({
        "ticker": ["BBB", "BBB", "CCC", "AAA"],
        "split_date": pd.to_datetime(
            ["2026-10-01", "2026-11-02", "2026-12-03", "2026-11-30"]
        ),
    })
    # Entry-day jumps precede the entry close; exit-day jumps are still held.
    assert marks.inside_holdings(events, windows).tolist() == [False, True, False, True]
    assert marks.inside_holdings(pd.DataFrame(), windows).empty


def _event(**overrides) -> dict:
    event = {
        "recorded_at": "2026-11-05T02:00:00+00:00",
        "ticker": "AAA",
        "event_type": marks.SPLIT,
        "event_date": "2026-11-03",
        "adjustment_factor": "0.5",
        "terminal_return": "",
        "source_url": "https://example.com/aaa-split",
        "note": "2-for-1, per the company's 8-K",
    }
    event.update(overrides)
    return event


def test_supplement_appends_validated_rows_and_keeps_earlier_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "supplement.csv"
    assert marks.load_supplement(path).empty
    marks.append_supplement_event(path, _event())
    first = path.read_bytes()
    digest = marks.supplement_digest(marks.load_supplement(path))
    marks.append_supplement_event(path, _event(
        recorded_at="2026-11-20T02:00:00+00:00",
        ticker="CCC",
        event_type=marks.TERMINAL_RETURN,
        event_date="2026-11-19",
        adjustment_factor="",
        terminal_return="0.012",
        note="cash merger at $50.60",
    ))
    assert path.read_bytes().startswith(first)
    frame = marks.load_supplement(path)
    assert frame["event_id"].tolist() == ["1", "2"]
    assert marks.supplement_digest(frame.head(1)) == digest
    assert marks.supplement_terminal_returns(frame) == {
        ("CCC", _day("2026-11-19")): 0.012
    }
    rows = marks.supplement_validation_rows(frame)
    assert rows.to_dict("records") == [{
        "ticker": "AAA",
        "split_date": _day("2026-11-03"),
        "validation_status": "CONFIRMED",
        "confirmed_action_type": "SPLIT",
        "confirmed_action_date": _day("2026-11-03"),
        "confirmed_adjustment_factor": 0.5,
        "primary_source": "https://example.com/aaa-split",
    }]

    with pytest.raises(ValueError, match="duplicates an earlier jump"):
        marks.append_supplement_event(path, _event(
            recorded_at="2026-11-21T02:00:00+00:00",
            event_type=marks.MARKET_MOVE,
            adjustment_factor="",
        ))
    assert marks.load_supplement(path)["event_id"].tolist() == ["1", "2"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recorded_at": "2026-11-05T02:00:00"}, "timezone-aware"),
        ({"ticker": "aaa"}, "upper-case"),
        ({"event_type": "DIVIDEND"}, "unsupported event_type"),
        ({"event_date": "2026-11-3"}, "YYYY-MM-DD"),
        ({"source_url": "see email"}, "source_url"),
        ({"adjustment_factor": "1"}, "SPLIT needs"),
        ({"event_type": marks.MARKET_MOVE}, "no numbers"),
        ({"event_type": marks.TERMINAL_RETURN, "adjustment_factor": "",
          "terminal_return": "-1.5"}, "terminal_return >= -1"),
    ],
)
def test_supplement_refuses_unsourced_or_malformed_events(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        marks.append_supplement_event(tmp_path / "supplement.csv", _event(**overrides))
    assert not (tmp_path / "supplement.csv").exists()


def test_supplement_rows_must_stay_in_order(tmp_path: Path) -> None:
    path = tmp_path / "supplement.csv"
    marks.append_supplement_event(path, _event())
    with pytest.raises(ValueError, match="goes back in time"):
        marks.append_supplement_event(path, _event(
            recorded_at="2026-11-01T00:00:00+00:00", ticker="BBB"
        ))
    edited = path.read_text().replace(",1,", ",7,", 1).replace("\n1,", "\n7,")
    path.write_text(edited)
    with pytest.raises(ValueError, match="event_id must be 1"):
        marks.load_supplement(path)


def test_carry_keeps_frozen_rows_and_audits_revisions() -> None:
    frozen = pd.DataFrame({
        "date": ["2026-10-01", "2026-10-02", "2026-10-05"],
        "close": [100.0, 101.0, 102.0],
        "volume": [10, 11, 12],
    })
    fresh = pd.DataFrame({
        "date": ["2026-10-01", "2026-10-02", "2026-10-06", "2026-10-07"],
        "close": [100.0, 101.5, 103.0, 104.0],
        "volume": [10, 11, 13, 14],
    })
    combined, audit = marks.carry_frozen_rows(
        fresh, frozen, "2026-10-05", ["close", "volume"]
    )
    assert combined["close"].tolist() == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert audit["frozen_rows_kept"] == 3
    assert audit["fresh_rows_appended"] == 2
    # 10-02 was revised and 10-05 vanished from the fresh download.
    assert audit["revised_frozen_rows"] == 2
    assert [row["date"] for row in audit["revised_sample"]] == [
        "2026-10-02", "2026-10-05"
    ]


def test_rows_digest_ignores_integer_versus_float_storage() -> None:
    as_int = pd.DataFrame({"date": ["2026-10-01", "2026-10-02"], "volume": [10, 11]})
    as_float = pd.DataFrame({
        "date": ["2026-10-02", "2026-10-01", "2026-10-05"],
        "volume": [11.0, 10.0, None],
    })
    assert marks.rows_digest_text(as_int, ["volume"], "2026-10-02") == (
        marks.rows_digest_text(as_float, ["volume"], "2026-10-02")
    )
    assert marks.rows_digest_text(as_float, ["volume"], "2026-10-05").endswith(
        "2026-10-05,nan"
    )


def test_split_like_moves_find_splits_on_moving_days_and_uneven_ratios() -> None:
    dates = pd.bdate_range("2026-07-01", periods=6)
    raw = pd.DataFrame({
        # 2:1 on a +4% day: outside the whole-factor tolerance, still reviewed.
        "AAA": [100.0, 101, 52.6, 53, 53.5, 54],
        # 3:2
        "BBB": [50.0, 50.5, 51, 34, 34.2, 34.1],
        # 1:10 reverse split
        "CCC": [2.0, 2.02, 20.4, 20.3, 20.2, 20.4],
        # an ordinary -12% day is not reviewed
        "DDD": [20.0, 17.6, 17.7, 17.8, 17.9, 18.0],
    }, index=dates)

    moves = marks.split_like_moves(raw)

    assert list(zip(moves["ticker"], moves["split_date"])) == [
        ("AAA", dates[2]), ("CCC", dates[2]), ("BBB", dates[3]),
    ]
    assert moves.set_index("ticker").loc["CCC", "reason"] == "COMMON_SPLIT_RATIO"
    assert moves.set_index("ticker").loc["AAA", "reason"] == "LARGE_MOVE"
    assert marks.split_like_moves(raw, ["AAA"], start=dates[3]).empty


def test_unexplained_moves_drop_those_a_resolved_event_explains() -> None:
    dates = pd.bdate_range("2026-07-01", periods=4)
    raw = pd.DataFrame({"AAA": [100.0, 50.0, 50, 50], "BBB": [10.0, 30.0, 30, 30]},
                       index=dates)
    validation = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "split_date": [dates[1], dates[1]],
        "validation_status": ["CONFIRMED", "UNRESOLVED_PRICE_JUMP"],
    })

    left = marks.unexplained_moves(raw, validation)

    assert left["ticker"].tolist() == ["BBB"]
