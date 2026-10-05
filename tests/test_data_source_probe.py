import importlib.util
from pathlib import Path

import pandas as pd

spec = importlib.util.spec_from_file_location(
    "data_source_probe", Path(__file__).resolve().parents[1] / "scripts/data_source_probe.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_probe_ledger_is_not_the_v1_ledger():
    assert "data_source_probe" in str(mod.common.QUOTA_LEDGER)
    assert "data_source_probe" in str(mod.common.RAW_INDEX)


def test_yahoo_csv_span_reads_month_minus_one():
    url = "http://real-chart.finance.yahoo.com/table.csv?s=AMTD&d=6&e=20&f=2014&g=d&a=2&b=4&c=1997&ignore=.csv"
    assert mod.yahoo_csv_span(url) == ("1997-03-04", "2014-07-20")
    assert mod.yahoo_csv_span("http://ichart.yahoo.com/table.csv?s=X&amp;d=8&amp;e=7&amp;f=2005") == (None, "2005-09-07")


def test_parse_yahoo_hp_rows_and_events():
    row = ('<tr><td class="yfnc_tabledata1" nowrap align="right">Jul 3, 2013</td>'
           + "".join(f'<td class="yfnc_tabledata1" align="right">{v}</td>' for v in
                     ("45.13", "45.32", "45.12", "45.20", "777,800", "44.90")) + "</tr>")
    ev = ('<tr><td class="yfnc_tabledata1" nowrap align="right">Jul 2, 2013</td>'
          '<td class="yfnc_tabledata1" align="center" colspan="6">0.25 Dividend</td></tr>')
    df, events = mod.parse_yahoo_hp(row + ev)
    assert len(df) == 1 and df.loc[0, "close"] == 45.20 and df.loc[0, "adj"] == 44.90
    assert events == [(pd.Timestamp("2013-07-02"), "0.25 Dividend")]


def test_parse_yahoo_history_store_splits_rows_from_events():
    text = ('xx"HistoricalPriceStore":{"prices":[{"date":1571751000,"open":1,"high":1,"low":1,"close":38.15,'
            '"volume":5,"adjclose":38.0},{"amount":0.3,"date":1565011800,"type":"DIVIDEND","data":0.3}],'
            '"isPending":false}yy')
    df, events = mod.parse_yahoo_history_store(text)
    assert list(df["date"]) == [pd.Timestamp("2019-10-22")]
    assert events == [(pd.Timestamp("2019-08-05"), "DIVIDEND:0.3")]
    assert mod.parse_yahoo_history_store("no store")[0].empty


def test_pick_spaced_keeps_latest_and_gap():
    s = ["20130705000000", "20130601000000", "20130301000000", "20121207000000", "20121026000000"]
    assert mod.pick_spaced(s, 80, 10) == ["20130705000000", "20130301000000", "20121207000000"]
    assert mod.pick_spaced(s, 80, 1) == ["20130705000000"]


def test_cmc_pick_rejects_reused_ticker_for_delisted_name():
    res = [{"identifier": "LIFE", "type": "stock", "url": "ethos"},
           {"identifier": "BMC.defunct.2013", "type": "stock", "url": "bmc-software"},
           {"identifier": "BMC", "type": "stock", "url": "bmc-new"}]
    assert mod.cmc_pick(res, "LIFE", 2014) is None
    assert mod.cmc_pick(res, "BMC", 2013)["url"] == "bmc-software"
    assert mod.cmc_pick(res, "BMC", 2019) is None
    assert mod.cmc_pick(res, "BMC", None)["url"] == "bmc-new"


def test_cmc_slugs():
    assert mod.cmc_slugs("BMC SOFTWARE INC") == ["bmc-software", "bmc"]
    assert mod.cmc_slugs("COMVERSE TECHNOLOGY INC/NY/") == ["comverse-technology", "comverse"]


def test_consecutive_returns_skip_gaps_and_agreement():
    sessions = pd.DatetimeIndex(pd.to_datetime(["2013-01-02", "2013-01-03", "2013-01-04", "2013-01-07"]))
    df = pd.DataFrame({"date": pd.to_datetime(["2013-01-02", "2013-01-03", "2013-01-07"]), "adj": [10.0, 11.0, 12.1]})
    r = mod.consecutive_returns(df, "adj", sessions)
    assert list(r.index) == [pd.Timestamp("2013-01-03")]     # 01-07 spans the missing 01-04
    ref = pd.Series([0.1, 0.0], index=pd.to_datetime(["2013-01-03", "2013-01-07"]))
    assert mod.agreement(r, ref) == (1, 1)
    assert mod.agreement(r + 0.01, ref) == (1, 0)


def test_classify_plan_text():
    z = mod.classify_plan_text("Existing Equity Interests shall be canceled and extinguished, and Holders will "
                               "not receive any distribution on account of such Interests.")
    assert z == {"zero": 1, "positive": 0}
    assert mod.classify_plan_text("Intercompany Interests shall be canceled and extinguished and Holders of "
                                  "Intercompany Interests will not receive any distribution")["zero"] == 0
    assert mod.classify_plan_text("holders of existing common stock will receive their pro rata share")["positive"] == 1


def test_parse_yahoo_table_2023_layout():
    row = ('<tr class="x"><td class="a"><span>Dec 22, 2023</span></td>'
           + "".join(f'<td class="b"><span>{v}</span></td>' for v in ("15.11", "15.30", "14.95", "15.00", "14.98",
                                                                       "20,096,000")) + "</tr>")
    ev = ('<tr class="x"><td class="a"><span>Dec 14, 2023</span></td><td class="c" colSpan="6">'
          '<strong>0.05</strong> <span>Dividend</span></td></tr>')
    df, events = mod.parse_yahoo_table(row + ev)
    assert df.loc[0, "close"] == 15.00 and df.loc[0, "adj"] == 14.98
    assert events == [(pd.Timestamp("2023-12-14"), "0.05 Dividend")]
