from pathlib import Path

import pandas as pd

from src.io.corporate_actions import extend_predecessor_price_histories


def test_symbol_change_extends_predecessor_without_removing_successor(tmp_path: Path) -> None:
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    pd.DataFrame([{
        "date": "2025-05-12", "ticker": "OLD", "open": 10.0, "high": 11.0,
        "low": 9.0, "close": 10.5, "volume": 100,
    }]).to_csv(price_dir / "old.csv", index=False)
    successor = pd.DataFrame([{
        "date": "2025-05-13", "ticker": "NEW", "open": 10.6, "high": 11.2,
        "low": 10.1, "close": 11.0, "volume": 120,
    }])
    successor.to_csv(price_dir / "new.csv", index=False)
    actions = tmp_path / "actions.csv"
    pd.DataFrame([{
        "predecessor": "OLD", "last_price_date": "2025-05-12",
        "successor": "NEW", "effective_date": "2025-05-13", "share_ratio": 1.0,
        "cash_per_share": 0.0, "source_url": "https://example.com/filing",
        "verified_at": "2026-07-19T00:00:00Z",
    }]).to_csv(actions, index=False)

    report = extend_predecessor_price_histories(actions, price_dir)

    extended = pd.read_csv(price_dir / "old.csv")
    assert report["results"][0]["rows_added"] == 1
    assert extended["date"].tolist() == ["2025-05-12", "2025-05-13"]
    assert extended.loc[1, "ticker"] == "OLD"
    assert extended.loc[1, "close"] == 11.0
    assert pd.read_csv(price_dir / "new.csv").equals(successor)
