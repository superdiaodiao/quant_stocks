from scripts.research_v14_afmd_quarterly_reports import (
    EXPECTED,
    derive_quarters,
)


def test_afmd_derives_q1_and_q4_without_even_splitting() -> None:
    parsed = {
        "2019_h1": {
            ("2018-01-01", "2018-06-30", "revenue"): 682_000.0,
            ("2018-04-01", "2018-06-30", "revenue"): 150_000.0,
            ("2018-01-01", "2018-06-30", "net_income"): -16_217_000.0,
            ("2018-04-01", "2018-06-30", "net_income"): -8_014_000.0,
            ("2019-01-01", "2019-06-30", "revenue"): 15_361_000.0,
            ("2019-04-01", "2019-06-30", "revenue"): 4_008_000.0,
            ("2019-01-01", "2019-06-30", "net_income"): -8_488_000.0,
            ("2019-04-01", "2019-06-30", "net_income"): -10_340_000.0,
        },
        "2019_q3": {
            "q3_current_revenue": 2_103_000.0,
            "q3_comparative_revenue": 306_000.0,
            "nine_month_current_revenue": 17_464_000.0,
            "nine_month_comparative_revenue": 988_000.0,
            "q3_current_net_income": -10_884_000.0,
            "q3_comparative_net_income": -12_020_000.0,
            "nine_month_current_net_income": -19_372_000.0,
            "nine_month_comparative_net_income": -28_237_000.0,
        },
        "2019_fy": {
            ("2018-01-01", "2018-12-31", "revenue"): 23_735_000.0,
            ("2018-01-01", "2018-12-31", "net_income"): -19_477_000.0,
            ("2019-01-01", "2019-12-31", "revenue"): 21_391_000.0,
            ("2019-01-01", "2019-12-31", "net_income"): -32_365_000.0,
        },
        "2020_h1": {
            ("2020-01-01", "2020-06-30", "revenue"): 8_069_000.0,
            ("2020-04-01", "2020-06-30", "revenue"): 2_934_000.0,
            ("2020-01-01", "2020-06-30", "net_income"): -20_527_000.0,
            ("2020-04-01", "2020-06-30", "net_income"): -12_238_000.0,
        },
        "2020_q3": {
            "q3_current_revenue": 10_545_000.0,
            "q3_comparative_revenue": 2_103_000.0,
            "nine_month_current_revenue": 18_614_000.0,
            "nine_month_comparative_revenue": 17_464_000.0,
            "q3_current_net_income": -5_966_000.0,
            "q3_comparative_net_income": -10_884_000.0,
            "nine_month_current_net_income": -26_493_000.0,
            "nine_month_comparative_net_income": -19_372_000.0,
        },
        "2020_fy": {
            ("2020-01-01", "2020-12-31", "revenue"): 28_360_000.0,
            ("2020-01-01", "2020-12-31", "net_income"): -41_366_000.0,
        },
        "2021_h1": {
            ("2021-01-01", "2021-06-30", "revenue"): 21_366_000.0,
            ("2021-04-01", "2021-06-30", "revenue"): 9_707_000.0,
            ("2021-01-01", "2021-06-30", "net_income"): -17_340_000.0,
            ("2021-04-01", "2021-06-30", "net_income"): -18_752_000.0,
        },
    }

    observed = {
        fiscal_end: (values["revenue"], values["net_income"])
        for fiscal_end, values in derive_quarters(parsed).items()
    }

    assert observed == EXPECTED
