"""Hand-reviewed CUSIP -> price-source map for Berkshire Hathaway's 13F holdings (docs/research_ledger_buffett.md
section 0.2 / 0.4.3). No prices here, only identifiers.

OpenFIGI (``quant.data.sources.openfigi``) maps the CUSIPs it still lists to their US composite ticker; everything it
does not know (mostly companies delisted or re-domiciled, whose CUSIP changed) is mapped here by hand from the issuer
name, the 13F class and the holding dates. Each entry: CUSIP -> list of segments ``(source, symbol, first, last)``
where ``source`` is 'yahoo' (current ticker, full history), 'wayback' (archive.org copy of Yahoo's old table.csv for
the ticker of the time), 'wiki' or 'alpaca' (SIP daily bars, 2016 on), and ``first`` / ``last`` (ISO dates or None) bound
the segment when one CUSIP needs two sources. 'wiki' is Nasdaq Data Link's WIKI PRICES extract in the frozen v1 cache
(2011-06-01 .. 2018-03-27, local, read only): a delisted name of the 2010s uses archive.org before 2011-06, WIKI to
2018-03 and Alpaca after. ``SKIP`` lists CUSIPs left unpriced on purpose (OTC or no daily source), with the
reason. Every mapping is then checked against the 13F-implied price at each report date (``buffett_prices``).
``DISTRIBUTIONS`` (added 2026-10-23 after the post-run review) lists spin-offs / share dividends a source's adjusted
series misses; ``buffett_prices.apply_distributions`` adds their value back on the ex-date.
"""
from __future__ import annotations

# filer typos in the information tables, corrected before positions are aggregated
CUSIP_FIXES = {
    "949773V107": "94973V107",   # WellPoint 2007-2009: "949773V 10 7" (one extra digit)
    "86816810F": "868168105",    # Superior Industries 2000-2001: last character mistyped
    "G6693N103": "G6683N103",    # Nu Holdings 2022: two digits swapped (G6683N103 from 2023)
}

# Yahoo writes share classes with a dash (OpenFIGI with a slash) and some tickers changed after OpenFIGI's record
FIGI_TO_YAHOO = {"HEI/A": "HEI-A", "LEN/B": "LEN-B", "BRK/B": "BRK-B"}

WK_A0, WK_A, WK_B = "2011-05-31", "2011-06-01", "2018-03-27"   # archive.org up to WK_A0, then WIKI's span
AL_A = "2018-03-28"                                             # Alpaca after WIKI ends

HAND = {
    # ---- large holdings no longer listed (or listed under another CUSIP)
    "375766102": [("wayback", "G", None, None)],               # Gillette, merged into P&G 2005-10-01
    "950817106": [("wayback", "WSC", None, None)],             # Wesco Financial, bought by Berkshire 2011-06
    "939640108": [("yahoo", "GHC", None, None)],               # Washington Post Cl B = Graham Holdings
    "50075N104": [("yahoo", "MDLZ", None, None)],              # Kraft Foods Inc. = Mondelez (renamed 2012-10)
    "12189T104": [("wayback", "BNI", None, None)],             # Burlington Northern Santa Fe, bought 2010-02
    # USG: Alpaca's USG bars before 2021-11 are zero-volume placeholders (then a reused ticker); the Alpaca segment is
    # bounded at the Knauf takeover close (2019-04-24) - fixed 2026-10-23 after the run, see ledger run log
    "903293405": [("wayback", "USG", None, WK_A0), ("wiki", "USG", WK_A, WK_B), ("alpaca", "USG", AL_A, "2019-04-24")],
    "035229103": [("wayback", "BUD", None, None)],             # Anheuser-Busch Cos, bought by InBev 2008-11
    "867914103": [("wayback", "STI", None, WK_A0), ("wiki", "STI", WK_A, WK_B)],      # SunTrust (sold by 2010)
    "82028K200": [("wayback", "SJR", None, None)],             # Shaw Communications Cl B (NYSE: SJR)
    "319963104": [("wayback", "FDC", None, None)],             # First Data (old), LBO 2007-09
    "029712106": [("wayback", "ASD", None, None)],             # American Standard Cos (Trane Inc. from 2007-11)
    "364730101": [("wayback", "GCI", None, WK_A0), ("wiki", "TGNA", WK_A, WK_B)],   # Gannett (old) = TEGNA (gone from Yahoo)
    "H1467J104": [("yahoo", "CB", None, None)],                # Chubb Ltd
    "25490A309": [("wiki", "DTV", WK_A, "2015-07-24")],        # DIRECTV, bought by AT&T 2015-07-24
    "25490A101": [("wiki", "DTV", WK_A, "2015-07-24")],        # DIRECTV Cl A (before the 2011 class change)
    "891027104": [("yahoo", "GL", None, None)],                # Torchmark = Globe Life (renamed 2019)
    "30231G102": [("yahoo", "XOM", None, None)],               # Exxon Mobil
    "462846106": [("yahoo", "IRM", None, None)],               # Iron Mountain (CUSIP changed with the REIT conversion)
    "20030N200": [("wayback", "CMCSK", None, None)],           # Comcast Cl A Special, folded into CMCSA 2015-11
    "531229607": [("alpaca", "LSXMK", None, None)],            # Liberty SiriusXM C (2016-2023)
    "531229789": [("alpaca", "LSXMK", None, None)],            # Liberty SiriusXM C (new CUSIP 2023-2024)
    "531229409": [("alpaca", "LSXMA", None, None)],            # Liberty SiriusXM A (2016-2023)
    "531229813": [("alpaca", "LSXMA", None, None)],            # Liberty SiriusXM A (new CUSIP 2023-2024)
    "480074103": [("wayback", "JNY", None, None)],             # Jones Apparel Group
    "404119109": [("wayback", "HCA", None, None)],             # HCA Inc. (old), LBO 2006-11
    "369604103": [("yahoo", "GE", None, None)],                # General Electric (old CUSIP before the 2021 reverse split)
    "390568103": [("wayback", "GLK", None, None)],             # Great Lakes Chemical, merged 2005-07
    "26483B106": [("yahoo", "MCO", None, None)],               # Dun & Bradstreet (old) = Moody's after the 2000 separation
    "26483E100": [("wayback", "DNB", None, None)],             # Dun & Bradstreet (new, 2000-2019)
    "G5480U104": [("yahoo", "LBTYA", None, None)],             # Liberty Global A
    "G5480U120": [("yahoo", "LBTYK", None, None)],             # Liberty Global C
    "16117M305": [("yahoo", "CHTR", None, None)],              # Charter (CUSIP before the 2016 merger)
    "G0750C108": [("yahoo", "AXTA", None, None)],              # Axalta
    "G0403H108": [("yahoo", "AON", None, None)],               # Aon plc
    "172967101": [("yahoo", "C", None, None)],                 # Citigroup (before the 2011 reverse split)
    "740189105": [("wiki", "PCP", WK_A, WK_B)],                # Precision Castparts, bought by Berkshire 2016-01
    "902124106": [("wayback", "TYC", None, None)],             # Tyco International (split up 2007-06)
    "00507V109": [("alpaca", "ATVI", None, None)],             # Activision Blizzard, bought by Microsoft 2023-10
    "92553P201": [("wiki", "VIAB", WK_A, WK_B)],               # Viacom B (2012-2015)
    "92927K102": [("wayback", "WBC", None, WK_A0), ("wiki", "WBC", WK_A, WK_B)],      # WABCO (sold by 2017-08)
    "929251106": [("wayback", "WBC", None, None)],             # WABCO (2007 CUSIP)
    "82968B103": [("alpaca", "SIRI", None, "2024-09-06")],     # Sirius XM Holdings (old, until the 2024-09 merger)
    "81211K100": [("yahoo", "SEE", None, None)],               # Sealed Air
    "167250109": [("wiki", "CBI", WK_A, WK_B)],                # Chicago Bridge & Iron (N.Y. registry shares)
    "689899102": [("wayback", "OSI", None, None)],             # Outback Steakhouse (OSI Restaurant Partners 2006)
    "67104A101": [("wayback", "OSI", None, None)],             # OSI Restaurant Partners, LBO 2007-06
    "637071101": [("yahoo", "NOV", None, None)],               # National Oilwell Varco
    "531229102": [("wiki", "LMCA", WK_A, "2016-04-15")],       # Liberty Media Corp A (2013-2016; recapitalised 2016-04)
    "530322106": [("wayback", "LMCA", None, None)],            # Liberty Media (Liberty Capital) A (2012-2013)
    "531229300": [("wayback", "LMCK", None, None)],            # Liberty Media Corp C (2014-2016)
    "862121100": [("alpaca", "STOR", None, None)],             # STORE Capital, bought 2023-02
    "92556H206": [("alpaca", "PARA", None, None)],             # Paramount Global B
    "61166W101": [("alpaca", "MON", None, "2018-06-06")],      # Monsanto, bought by Bayer 2018-06-07 (bound 2026-10-23)
    "881624209": [("yahoo", "TEVA", None, None)],              # Teva ADR
    "G4776G101": [("yahoo", "TT", None, None)],                # Ingersoll-Rand Co Ltd = Trane Technologies' history
    "G47766101": [("yahoo", "TT", None, None)],                # Ingersoll-Rand (2007 CUSIP)
    "G47791101": [("yahoo", "TT", None, None)],                # Ingersoll-Rand plc (2012)
    "539320101": [("wayback", "LIZ", None, None)],             # Liz Claiborne
    "720279108": [("wayback", "PIR", None, None)],             # Pier 1 Imports
    "71646E100": [("wayback", "PTR", None, None)],             # PetroChina ADR
    "62985Q101": [("wayback", "NLC", None, None)],             # Nalco Holding, bought by Ecolab 2011-12
    "G85158106": [("yahoo", "STNE", None, None)],              # StoneCo
    "81760N109": [("wayback", "SVM", None, None)],             # ServiceMaster (old), LBO 2007-07
    "37733W105": [("yahoo", "GSK", None, None)],               # GlaxoSmithKline ADR
    "G6683N103": [("yahoo", "NU", None, None)],                # Nu Holdings
    "531229854": [("yahoo", "FWONK", None, None)],             # Liberty Media C Formula One / Media group
    "531229870": [("yahoo", "FWONA", None, None)],             # Liberty Media A Formula One / Media group
    "36225X100": [("wayback", "GPU", None, None)],             # GPU Inc., bought by FirstEnergy 2001-11
    "531229722": [("yahoo", "LLYVK", None, None)],             # Liberty Live C (tracking stock 2023-2025)
    "531229748": [("yahoo", "LLYVA", None, None)],             # Liberty Live A
    "90130A101": [("wiki", "FOXA", WK_A, WK_B)],               # Twenty-First Century Fox A (2015-2017)
    "210371100": [("wayback", "CEG", None, None)],             # Constellation Energy Group (old)
    "911596104": [("wayback", "USB", None, None)],             # U.S. Bancorp (old, before the 2001 Firstar merger)
    "529771107": [("wayback", "LXK", None, None)],             # Lexmark
    "892893108": [("wayback", "TT", None, None)],              # Trane Inc. (2007-2008; TT reused by Trane Technologies 2020)
    "756577102": [("alpaca", "RHT", None, None)],              # Red Hat, bought by IBM 2019-07
    "171232101": [("wayback", "CB", None, None)],              # Chubb Corp (old)
    "989390109": [("wayback", "ZNT", None, None)],             # Zenith National Insurance
    "260561105": [("wayback", "DJ", None, None)],              # Dow Jones & Co, bought by News Corp 2007-12
    "278058102": [("yahoo", "ETN", None, None)],               # Eaton (before the 2012 re-domicile)
    "94973V107": [("yahoo", "ELV", None, None)],               # WellPoint = Elevance Health
    "85571Q102": [("wiki", "STRZA", WK_A, WK_B)],              # Starz
    "584404107": [("wiki", "MEG", WK_A, WK_B)],                # Media General (2012-2014)
    "58441K100": [("wiki", "MEG", WK_A, WK_B)],                # Media General (new, 2015-2016)
    "52729N100": [("wayback", "LVLT", None, None)],            # Level 3 Communications
    "067901108": [("yahoo", "B", None, None)],                 # Barrick (ticker GOLD until 2025, B after)
    "G9001E102": [("yahoo", "LILA", None, None)],              # Liberty Latin America A
    "G9001E128": [("yahoo", "LILAK", None, None)],             # Liberty Latin America C
    "G5480U138": [("yahoo", "LILA", None, None)],              # Liberty Global LiLAC A (tracking stock 2015-2017)
    "G5480U153": [("yahoo", "LILAK", None, None)],             # Liberty Global LiLAC C
    "428040109": [("wayback", "HRZ", None, None)],             # Hertz Corp (old), bought by Ford 2001
    "820286102": [("wayback", "SHX", None, None)],             # Shaw Industries, bought by Berkshire 2001-01
    "478129109": [("wayback", "JM", None, None)],              # Johns Manville, bought by Berkshire 2001-02
    "50076Q106": [("wiki", "KRFT", WK_A, WK_B)],               # Kraft Foods Group, merged into Kraft Heinz 2015-07
    "G7709Q104": [("yahoo", "RPRX", None, None)],              # Royalty Pharma
    "438516106": [("yahoo", "HON", None, None)],               # Honeywell
    "676220106": [("yahoo", "ODP", None, None)],               # Office Depot = ODP Corp
    "264399106": [("yahoo", "DUK", None, None)],               # Duke Energy (old CUSIP before the 2006 Cinergy merger)
    "902911106": [("wayback", "UST", None, None)],             # UST Inc.
    "242370104": [("wayback", "DF", None, None)],              # Dean Foods
    "G0176J109": [("yahoo", "ALLE", None, None)],              # Allegion
    "360921100": [("wayback", "FBN", None, None)],             # Furniture Brands
    "868168105": [("yahoo", "SUP", None, None)],               # Superior Industries
    "25470M109": [("wiki", "DISH", WK_A, WK_B)],               # DISH Network (2013)
    "30219G108": [("wiki", "ESRX", WK_A, WK_B)],               # Express Scripts (2014)
    "895953107": [("yahoo", "YUM", None, None)],               # Tricon Global Restaurants = Yum! Brands
    "523768109": [("yahoo", "LEE", None, None)],               # Lee Enterprises
}

SKIP = {
    "200334100": "Comdisco Holding: OTC after the 2002 reorganisation, no daily source",
    "81211K209": "Sealed Air Series A convertible preferred: no daily source",
    "693622102": "PS Group Holdings: no daily source (tiny, 1999-2000)",
    "972487102": "WilTel Communications: no daily source (tiny, 2003)",
    "760719104": "Republic New York: no daily source (tiny, 2000)",
    "493482103": "Keystone Financial: no daily source (tiny, 2000)",
}


# distributions a source's adjusted series misses (found by the post-run review, 2026-10-23; applied by
# buffett_prices.apply_distributions to the day's total return): cusip -> [(ex-date, shares distributed per share
# held, price source of the distributed share as (source, symbol) or None = the parent's own close that day, note)]
_LIVE_A = ("2023-08-04", 0.25, ("yahoo", "LLYVA"), "Liberty Live: 0.25 LLYVA per LSXMA (Alpaca misses it)")
_LIVE_K = ("2023-08-04", 0.25, ("yahoo", "LLYVK"), "Liberty Live: 0.25 LLYVK per LSXMK (Alpaca misses it)")
DISTRIBUTIONS = {
    "531229102": [("2014-07-24", 2.0, None, "Liberty Media C-share dividend: 2 LMCK per LMCA (WIKI misses it; no local "
                                            "LMCK close, the C shares are priced at LMCA's close)"),
                  ("2014-11-04", 0.25, ("yahoo", "LBRDA"), "Liberty Broadband spin-off: 1 LBRDA per 4 LMCA (WIKI "
                                                            "misses it)")],
    "531229409": [_LIVE_A], "531229813": [_LIVE_A],
    "531229607": [_LIVE_K], "531229789": [_LIVE_K],
}

# sources whose 13F price check fails for an explained reason (listed before registration); their series are used
ACCEPT = {
    "26483B106": "Dun & Bradstreet (old), priced by Yahoo's MCO: Yahoo scales the closes before the 2000-09-30 "
                 "separation by the spin-off (13F price / close = 1.332 at all 4 report dates, a constant); the "
                 "adjusted close carries the spin-off value (no drop on 2000-10-02)",
}


def spec_for(cusip: str, figi_ticker: str | None) -> list[tuple] | None:
    """The price segments of ``cusip``: the hand entry if any, else Yahoo under the OpenFIGI ticker, else None."""
    if cusip in SKIP:
        return None
    if cusip in HAND:
        return list(HAND[cusip])
    if figi_ticker:
        return [("yahoo", FIGI_TO_YAHOO.get(figi_ticker, figi_ticker.replace("/", "-")), None, None)]
    return None
