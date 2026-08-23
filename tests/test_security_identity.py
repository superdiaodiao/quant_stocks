import pandas as pd
import pytest

from src.io.security_identity import (
    normalize_universe_symbols,
    normalize_point_in_time_tickers,
    remap_weights_after_issuer_rename,
    split_reused_ticker_price_histories,
)


def test_reused_ticker_is_mapped_in_historical_universe_only():
    identities = pd.DataFrame({
        "provider_ticker": ["CORZ"],
        "historical_ticker": ["CORZ_PRE2024"],
        "last_historical_date": pd.to_datetime(["2024-01-23"]),
        "current_ticker_first_date": pd.to_datetime(["2024-01-24"]),
        "source_url": ["https://example.test/corz"],
        "verified_at": ["2026-07-30T00:00:00Z"],
        "identity_type": ["ticker_reuse"],
    })

    assert normalize_universe_symbols(
        {"CORZ", "A"}, pd.Timestamp("2024-01-01"), identities
    ) == {"CORZ_PRE2024", "A"}
    assert normalize_universe_symbols(
        {"CORZ", "A"}, pd.Timestamp("2024-02-01"), identities
    ) == {"CORZ", "A"}


def test_old_provider_history_is_mapped_to_historical_ticker(tmp_path):
    mapping = tmp_path / "identity.csv"
    mapping.write_text(
        "provider_ticker,historical_ticker,last_historical_date,current_ticker_first_date,source_url,verified_at\n"
        "RDUS,SCHN,2023-08-31,2023-09-01,https://www.sec.gov/example,2026-07-19T00:00:00Z\n"
    )
    source = pd.DataFrame({
        "ticker": ["RDUS", "RDUS"],
        "period_end": ["2023-08-31", "2023-11-30"],
    })

    result = normalize_point_in_time_tickers(source, mapping)

    assert result["ticker"].tolist() == ["SCHN", "RDUS"]


def test_same_issuer_rename_retains_current_ticker_financial_continuity(
    tmp_path,
):
    mapping = tmp_path / "identity.csv"
    mapping.write_text(
        "provider_ticker,historical_ticker,last_historical_date,current_ticker_first_date,source_url,verified_at,identity_type\n"
        "NEW,OLD,2025-06-30,2025-07-01,https://www.sec.gov/example,2026-07-30T00:00:00Z,issuer_rename\n"
    )
    source = pd.DataFrame({
        "ticker": ["NEW", "NEW"],
        "period_end": ["2025-06-30", "2025-09-30"],
    })

    result = normalize_point_in_time_tickers(source, mapping)

    assert result["ticker"].tolist() == ["NEW", "NEW", "OLD"]
    assert result.loc[result["ticker"].eq("OLD"), "period_end"].tolist() == [
        "2025-06-30"
    ]


def test_reverse_merger_financial_identity_uses_filing_cutover(tmp_path):
    mapping = tmp_path / "identity.csv"
    mapping.write_text(
        "provider_ticker,historical_ticker,last_historical_date,current_ticker_first_date,source_url,verified_at,identity_type\n"
        "NEGG,LLIT,2021-05-19,2021-05-20,https://www.sec.gov/example,2026-08-14T00:00:00Z,reverse_merger\n"
    )
    source = pd.DataFrame({
        "ticker": ["NEGG", "NEGG", "NEGG"],
        "period_end": ["2020-12-31", "2020-12-31", "2021-06-30"],
        "available_date": ["2021-03-31", "2022-04-28", "2021-08-01"],
        "value": [1.0, 2.0, 3.0],
    })

    result = normalize_point_in_time_tickers(source, mapping)

    assert result["ticker"].tolist() == ["LLIT", "NEGG", "NEGG"]
    assert result["value"].tolist() == [1.0, 2.0, 3.0]


def test_reverse_merger_maps_provider_symbol_in_pre_cutover_universe():
    identities = pd.DataFrame({
        "provider_ticker": ["NEGG"],
        "historical_ticker": ["LLIT"],
        "last_historical_date": pd.to_datetime(["2021-05-19"]),
        "current_ticker_first_date": pd.to_datetime(["2021-05-20"]),
        "source_url": ["https://www.sec.gov/example"],
        "verified_at": ["2026-08-14T00:00:00Z"],
        "identity_type": ["reverse_merger"],
    })

    assert normalize_universe_symbols(
        {"NEGG"}, pd.Timestamp("2021-05-19"), identities
    ) == {"LLIT"}
    assert normalize_universe_symbols(
        {"NEGG"}, pd.Timestamp("2021-05-20"), identities
    ) == {"NEGG"}


def test_price_history_split_is_idempotent_and_preserves_existing_history(
    tmp_path,
):
    mapping = tmp_path / "identity.csv"
    mapping.write_text(
        "provider_ticker,historical_ticker,last_historical_date,current_ticker_first_date,source_url,verified_at\n"
        "NEW,OLD,2025-06-30,2025-07-01,https://www.sec.gov/example,2026-07-30T00:00:00Z\n"
    )
    price_dir = tmp_path / "price"
    price_dir.mkdir()
    pd.DataFrame({
        "date": ["2025-06-27", "2025-06-30", "2025-07-01"],
        "ticker": ["NEW", "NEW", "NEW"],
        "close": [10.0, 11.0, 12.0],
    }).to_csv(price_dir / "new.csv", index=False)
    pd.DataFrame({
        "date": ["2025-06-26"],
        "ticker": ["OLD"],
        "close": [9.0],
    }).to_csv(price_dir / "old.csv", index=False)

    first = split_reused_ticker_price_histories(mapping, price_dir)
    second = split_reused_ticker_price_histories(mapping, price_dir)

    historical = pd.read_csv(price_dir / "old.csv")
    current = pd.read_csv(price_dir / "new.csv")
    assert historical["date"].tolist() == [
        "2025-06-26", "2025-06-27", "2025-06-30"
    ]
    assert historical["ticker"].unique().tolist() == ["OLD"]
    assert current["date"].tolist() == ["2025-07-01"]
    assert current["ticker"].unique().tolist() == ["NEW"]
    assert first[0]["historical_rows"] == second[0]["historical_rows"] == 3
    assert second[0]["historical_rows_extracted"] == 0


def test_weights_follow_same_issuer_rename_only_after_effective_date():
    weights = pd.Series({"OLD": 0.3, "NEW": 0.2, "REUSED": 0.1})
    transitions = pd.DataFrame({
        "provider_ticker": ["NEW", "CURRENT"],
        "historical_ticker": ["OLD", "REUSED"],
        "current_ticker_first_date": pd.to_datetime([
            "2025-07-01", "2025-07-01"
        ]),
        "identity_type": ["issuer_rename", "ticker_reuse"],
    })

    before = remap_weights_after_issuer_rename(
        weights, pd.Timestamp("2025-06-30"),
        transitions.loc[transitions["identity_type"].eq("issuer_rename")],
    )
    after = remap_weights_after_issuer_rename(
        weights, pd.Timestamp("2025-07-01"),
        transitions.loc[transitions["identity_type"].eq("issuer_rename")],
    )

    pd.testing.assert_series_equal(before, weights)
    assert after["OLD"] == 0.0
    assert after["NEW"] == pytest.approx(0.5)
    assert after["REUSED"] == weights["REUSED"]
