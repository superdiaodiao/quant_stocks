import pandas as pd

from scripts.sec_sina_alias_price_import import (
    _contiguous_sec_validation,
    _select_candidates,
)


def _frame(ticker: str, dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "ticker": ticker,
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.0,
        "volume": 100,
    })


def test_contiguous_sec_validation_accepts_unique_cik_next_session() -> None:
    candidate = {"cik": "123", "sec_issuers": [{"cik": "0000000123"}]}
    result = _contiguous_sec_validation(
        candidate,
        _frame("NEXT", ["2025-01-06"]),
        _frame("OLD", ["2025-01-03"]),
        {"sessions": 0},
    )
    assert result["passed"] is True
    assert result["terminal_tail_gap_days"] == 3


def test_contiguous_sec_validation_rejects_cik_mismatch_or_long_gap() -> None:
    local = _frame("OLD", ["2025-01-03"])
    source = _frame("NEXT", ["2025-01-20"])
    assert _contiguous_sec_validation(
        {"cik": "123", "sec_issuers": [{"cik": "456"}]},
        source,
        local,
        {"sessions": 0},
    )["passed"] is False
    assert _contiguous_sec_validation(
        {"cik": "123", "sec_issuers": [{"cik": "123"}]},
        source,
        local,
        {"sessions": 0},
    )["passed"] is False


def test_multiple_successors_require_an_explicit_override_for_selection() -> None:
    probe = {
        "results": [
            {
                "ticker": "OLD",
                "status": "ok",
                "matches": [{"cik": "123"}],
                "issuers": [
                    {
                        "cik": "123",
                        "current_tickers": ["NEW", "NEWW"],
                    }
                ],
            }
        ]
    }
    assert _select_candidates(probe) == []
    candidates = _select_candidates(
        probe,
        allow_multiple_successors=True,
        successor_overrides={"old": "new"},
    )
    assert [row["successor_ticker"] for row in candidates] == ["NEW"]


def test_historical_sec_display_alias_can_be_selected_by_override() -> None:
    probe = {
        "results": [{
            "ticker": "BCAN",
            "status": "ok",
            "search_url": "https://efts.sec.gov/LATEST/search-index?q=BCAN",
            "search_payload_sha256": "a" * 64,
            "matches": [{
                "cik": "0001888151",
                "display_name": (
                    "Femto Technologies Inc. (BCAN, FMTO) "
                    "(CIK 0001888151)"
                ),
            }],
            "issuers": [{
                "cik": "0001888151",
                "current_tickers": ["FMTOF"],
                "display_name": (
                    "Femto Technologies Inc. (FMTO, FMTOF) "
                    "(CIK 0001888151)"
                ),
            }],
        }]
    }

    selected = _select_candidates(
        probe, successor_overrides={"bcan": "fmto"}
    )

    assert len(selected) == 1
    assert selected[0]["historical_ticker"] == "BCAN"
    assert selected[0]["successor_ticker"] == "FMTO"
    assert selected[0]["cik"] == "0001888151"
    assert selected[0]["successor_resolution_scope"] == (
        "sec_search_historical_display_alias_override"
    )


def test_unproven_historical_alias_override_is_rejected() -> None:
    probe = {
        "results": [{
            "ticker": "OLD",
            "status": "ok",
            "matches": [{
                "cik": "0000000123",
                "display_name": "Issuer (OLD) (CIK 0000000123)",
            }],
            "issuers": [{
                "cik": "0000000123",
                "current_tickers": ["CURRENT"],
                "display_name": "Issuer (CURRENT) (CIK 0000000123)",
            }],
        }]
    }

    assert _select_candidates(
        probe, successor_overrides={"OLD": "UNSEEN"}
    ) == []
