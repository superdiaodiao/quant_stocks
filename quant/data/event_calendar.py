"""Calendar events known in advance: scheduled FOMC announcement days, unscheduled NYSE weekday closures and the
scheduled NYSE holidays derived from a session calendar.

Extracted unchanged from studies/calendar.py in the slimming pass (``FOMC_DATES``, ``UNSCHEDULED_CLOSURES``,
``parse_fed_pages`` with its label parser, ``scheduled_holidays``). The folder of the downloaded Federal Reserve
pages is an argument of ``parse_fed_pages`` (no default).
"""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path

import pandas as pd

# Weekday NYSE closures that were not scheduled holidays (excluded from the holiday list).
UNSCHEDULED_CLOSURES = (
    "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",   # September 11 attacks
    "2004-06-11",                                             # President Reagan national day of mourning
    "2007-01-02",                                             # President Ford national day of mourning
    "2012-10-29", "2012-10-30",                               # Hurricane Sandy
    "2018-12-05",                                             # President G.H.W. Bush national day of mourning
    "2025-01-09",                                             # President Carter national day of mourning
)

# Scheduled FOMC meeting announcement days (last day of the meeting). Source: Federal Reserve,
# https://www.federalreserve.gov/monetarypolicy/fomchistorical{YEAR}.htm (1999-2020) and
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (2021-2026), downloaded 2026-10-05.
# Unscheduled meetings, conference calls, notation votes and the cancelled 2020-03-17/18 meeting are excluded.
FOMC_DATES = (
    "1999-02-03", "1999-03-30", "1999-05-18", "1999-06-30", "1999-08-24", "1999-10-05", "1999-11-16", "1999-12-21",
    "2000-02-02", "2000-03-21", "2000-05-16", "2000-06-28", "2000-08-22", "2000-10-03", "2000-11-15", "2000-12-19",
    "2001-01-31", "2001-03-20", "2001-05-15", "2001-06-27", "2001-08-21", "2001-10-02", "2001-11-06", "2001-12-11",
    "2002-01-30", "2002-03-19", "2002-05-07", "2002-06-26", "2002-08-13", "2002-09-24", "2002-11-06", "2002-12-10",
    "2003-01-29", "2003-03-18", "2003-05-06", "2003-06-25", "2003-08-12", "2003-09-16", "2003-10-28", "2003-12-09",
    "2004-01-28", "2004-03-16", "2004-05-04", "2004-06-30", "2004-08-10", "2004-09-21", "2004-11-10", "2004-12-14",
    "2005-02-02", "2005-03-22", "2005-05-03", "2005-06-30", "2005-08-09", "2005-09-20", "2005-11-01", "2005-12-13",
    "2006-01-31", "2006-03-28", "2006-05-10", "2006-06-29", "2006-08-08", "2006-09-20", "2006-10-25", "2006-12-12",
    "2007-01-31", "2007-03-21", "2007-05-09", "2007-06-28", "2007-08-07", "2007-09-18", "2007-10-31", "2007-12-11",
    "2008-01-30", "2008-03-18", "2008-04-30", "2008-06-25", "2008-08-05", "2008-09-16", "2008-10-29", "2008-12-16",
    "2009-01-28", "2009-03-18", "2009-04-29", "2009-06-24", "2009-08-12", "2009-09-23", "2009-11-04", "2009-12-16",
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23", "2010-08-10", "2010-09-21", "2010-11-03", "2010-12-14",
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22", "2011-08-09", "2011-09-21", "2011-11-02", "2011-12-13",
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20", "2012-08-01", "2012-09-13", "2012-10-24", "2012-12-12",
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19", "2013-07-31", "2013-09-18", "2013-10-30", "2013-12-18",
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18", "2014-07-30", "2014-09-17", "2014-10-29", "2014-12-17",
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17", "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15", "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14", "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13", "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19", "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    "2020-01-29", "2020-04-29", "2020-06-10", "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16", "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)

MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"), 1)}
MON3 = {k[:3]: v for k, v in MONTHS.items()}


def scheduled_holidays(sessions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Weekdays between the first and last session with no trading, minus unscheduled closures."""
    wd = pd.bdate_range(sessions[0], sessions[-1])
    missing = wd.difference(sessions)
    return missing.difference(pd.DatetimeIndex(UNSCHEDULED_CLOSURES))


# ======================================================================== FOMC source parsing (data check / test)

def _meeting_last_day(year: int, label: str) -> dt.date:
    """'February 1-2' / 'March 30' / 'Apr/May 30-1' / 'July 31-August 1' / 'March 19-20*' -> last meeting day."""
    label = label.strip().rstrip("*").strip()
    m = re.match(r"([A-Za-z]+) (\d+)-([A-Za-z]+) (\d+)$", label)
    if m:
        return dt.date(year, MONTHS[m.group(3)], int(m.group(4)))
    mon, days = label.split(" ", 1)
    month = MON3[mon.split("/")[1][:3]] if "/" in mon else MONTHS[mon]
    return dt.date(year, month, int(days.strip().rstrip("*").split("-")[-1]))


def parse_fed_pages(raw_dir: Path, first: int = 1999, last: int = 2026) -> list[str]:
    """Re-derive the scheduled announcement days from the downloaded Federal Reserve pages in ``raw_dir``."""
    out = []
    for y in range(first, min(last, 2020) + 1):
        t = (raw_dir / f"fomchistorical{y}.htm").read_text()
        heads = [re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", "", m.group(1)))).strip()
                 for m in re.finditer(r"<h5[^>]*>(.*?)</h5>", t, re.S)]
        days = []
        for h in heads:
            if not re.search(r"\bMeeting - ", h) or re.search(r"unscheduled|cancelled|notation", h):
                continue
            d = _meeting_last_day(y, h.split(" Meeting")[0])
            if days and (d - days[-1]).days == 1:      # 2003-09-15 / 09-16 listed separately: one meeting
                days[-1] = d
            else:
                days.append(d)
        out += days
    if last >= 2021:
        t = (raw_dir / "fomccalendars.htm").read_text()
        for m in re.finditer(r'<h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4>', t):
            y = int(m.group(1))
            if not (max(first, 2021) <= y <= last):
                continue
            seg = t[m.end(): m.end() + 60000]
            nxt = seg.find("FOMC Meetings</a></h4>")
            seg = seg[: nxt if nxt > 0 else None]
            months = [x.strip() for x in re.findall(r"fomc-meeting__month[^>]*>\s*(?:<strong>)?([^<]+)", seg)]
            dates = [x.strip() for x in re.findall(r"fomc-meeting__date[^>]*>\s*([^<]+)", seg)]
            for mo, da in zip(months, dates):
                if re.search("unscheduled|notation|cancel", da):
                    continue
                out.append(_meeting_last_day(y, f"{mo} {da}"))
    return sorted(d.isoformat() for d in out)
