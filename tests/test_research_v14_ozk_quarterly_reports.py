from pathlib import Path

import pandas as pd

from scripts.research_v14_ozk_quarterly_reports import (
    _archive_url,
    _parse_quarter,
)


def test_ozk_archive_url_is_immutable_wayback_capture() -> None:
    assert _archive_url("20211023011004", "example") == (
        "https://web.archive.org/web/20211023011004id_/"
        "https://ir.ozk.com/news-releases/news-release-details/example"
    )


def test_ozk_registry_preserves_late_2020q1_comparative_availability() -> None:
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/ozk_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "available_date"],
    )
    assert len(registry) == 15
    late = registry.loc[registry["fiscal_end"].eq(pd.Timestamp("2020-03-31"))]
    assert len(late) == 1
    assert late.iloc[0]["available_date"] == pd.Timestamp("2021-04-22")
    assert not registry.duplicated(["fiscal_end", "available_date"]).any()


def test_ozk_real_archive_parser_uses_non_fte_bank_revenue() -> None:
    path = Path(
        "output/research_only/v14/ozk_ir_quarterly_reports_2018_2021/raw/"
        "20211023011004_bank-ozk-announces-first-quarter-2019-earnings.html"
    )
    if not path.exists():
        return
    values = _parse_quarter(path, pd.Timestamp("2019-03-31"))
    assert values == {
        "revenue": 249_960_000.0,
        "net_income": 110_712_000.0,
        "net_interest_income": 225_888_000.0,
        "noninterest_income": 24_072_000.0,
    }
