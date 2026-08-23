from scripts.research_v14_town_annual_quarterly import EXPECTED, parse_quarter_block


def test_town_quarter_block_derives_bank_revenue_and_profit() -> None:
    block = " ".join((
        "2019 Fourth Third Second First",
        "Interest income $ 116,475 $ 119,637 $ 117,883 $ 113,830",
        "Interest expense 26,516 28,534 28,064 26,356",
        "Provision for loan losses 3,601 1,508 2,824 1,438",
        "Noninterest income 49,712 54,845 54,718 47,158",
        "Net gain on investment securities — (69) — (776)",
        "Noninterest expense 92,336 97,287 96,556 92,123",
        "Net income 35,948 39,400 36,242 32,084",
        "Noncontrolling interest (873) (1,741) (1,604) (673)",
    ))

    parsed = parse_quarter_block(block)

    assert parsed["revenue"] == [
        EXPECTED["2019-12-31"][0], EXPECTED["2019-09-30"][0],
        EXPECTED["2019-06-30"][0], EXPECTED["2019-03-31"][0],
    ]
    assert parsed["net_income"] == [
        EXPECTED["2019-12-31"][1], EXPECTED["2019-09-30"][1],
        EXPECTED["2019-06-30"][1], EXPECTED["2019-03-31"][1],
    ]
