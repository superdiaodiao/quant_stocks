import zipfile

from scripts.sec_inline_xbrl_target_probe import parse_inline_xbrl, probe_targets


def test_inline_xbrl_parser_applies_scale_and_ignores_segment_flag(tmp_path):
    html = """
    <html><body>
      <xbrli:context id="duration">
        <xbrli:period><xbrli:startDate>2020-01-01</xbrli:startDate>
        <xbrli:endDate>2020-12-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="duration"
        unitRef="USD" scale="3" decimals="-3">19,000</ix:nonFraction>
    </body></html>
    """
    archive = tmp_path / "filing-xbrl.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("filing.htm", html)

    member, facts = parse_inline_xbrl(archive)

    assert member == "filing.htm"
    assert len(facts) == 1
    assert facts[0]["name"] == "us-gaap:NetIncomeLoss"
    assert facts[0]["start"] == "2020-01-01"
    assert facts[0]["end"] == "2020-12-31"
    assert facts[0]["value"] == 19_000_000
    assert facts[0]["segmented"] is False


def test_parser_falls_back_to_classic_xbrl_instance(tmp_path):
    instance = """<?xml version="1.0"?>
    <xbrl xmlns="http://www.xbrl.org/2003/instance"
          xmlns:us-gaap="http://fasb.org/us-gaap/2015-01-31">
      <context id="duration"><entity><identifier scheme="x">1</identifier></entity>
        <period><startDate>2014-01-01</startDate><endDate>2014-12-31</endDate></period>
      </context>
      <unit id="USD"><measure>iso4217:USD</measure></unit>
      <us-gaap:NetIncomeLoss contextRef="duration" unitRef="USD"
        decimals="-3">-784000</us-gaap:NetIncomeLoss>
    </xbrl>"""
    archive = tmp_path / "classic-xbrl.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("report.htm", "<html><body>No inline facts</body></html>")
        target.writestr("issuer-20141231.xml", instance)

    member, facts = parse_inline_xbrl(archive)

    assert member == "issuer-20141231.xml"
    assert len(facts) == 1
    assert facts[0]["name"] == "us-gaap:NetIncomeLoss"
    assert facts[0]["value"] == -784000
    assert facts[0]["end"] == "2014-12-31"


def test_q4_probe_does_not_treat_equal_annual_value_as_direct_quarter(tmp_path):
    html = """
    <xbrli:context id="annual"><xbrli:period>
      <xbrli:startDate>2019-12-29</xbrli:startDate>
      <xbrli:endDate>2020-12-26</xbrli:endDate>
    </xbrli:period></xbrli:context>
    <ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="annual"
      unitRef="USD" scale="3" sign="-">24,499</ix:nonFraction>
    """
    archive = tmp_path / "annual-xbrl.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("filing.htm", html)

    report = probe_targets(
        archive,
        [{
            "fiscal_end": "2020-12-26",
            "value": -24_499_000,
            "concept": "derived_q4:NetIncomeLoss",
        }],
        tmp_path / "probe.json",
    )

    assert report["targets"][0]["candidate_count"] == 0
    assert report["targets"][0]["exact_value_match_count"] == 0
