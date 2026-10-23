"""Berkshire Hathaway's 13F-HR filings (CIK 1067983) since 1999: the filing list, the full submission text of every
original and amendment, the information-table parsers (the pre-2013 plain-text tables and the XML tables), the
cover-page facts (report period, amendment type, the table's own entry / value totals for validation) and the
"public at the filing date" holding states the buffett study trades on (docs/research_ledger_buffett.md, section 0).

Requests use ``quant.data.sec_alt.sec_get`` (the SEC contact User-Agent from src/io/sec_contact.py, never printed;
at most 3 requests a second) and are cached under the caller's raw folder (``research_cache/buffett/raw/13f``).

Holding states (``holding_events``): an original 13F-HR for a report period later than every period seen so far
starts a new state; a 13F-HR/A for that same latest period changes it from its own filing date ("adds new holdings
entries" appends rows, "is a restatement" replaces them); originals or amendments for an older period are stale and
ignored. Each state is tradable at the close of the first session strictly after its filing date.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.sec_alt import sec_get

CIK = 1067983
FORMS = ("13F-HR", "13F-HR/A")
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}.txt"


# ======================================================================== filing list

def submissions(raw: Path, cik: int = CIK) -> list[dict]:
    """The EDGAR submissions JSON blocks of ``cik`` (recent + every older page), cached in ``raw``."""
    j = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", raw / f"submissions_{cik}.json"))
    blocks = [j["filings"]["recent"]]
    for f in j["filings"].get("files", []):
        blocks.append(json.loads(sec_get(f"https://data.sec.gov/submissions/{f['name']}", raw / f["name"])))
    return blocks


def filing_list(blocks: list[dict], end: str) -> pd.DataFrame:
    """Every 13F-HR / 13F-HR/A with filing date <= ``end``: accession, form, filing_date, acceptance, report_date,
    sorted by (filing_date, acceptance, accession). Paper filings (13FCONP) are not public content and are left out."""
    rows = []
    for b in blocks:
        for k in range(len(b["form"])):
            if b["form"][k] in FORMS:
                rows.append({"accession": b["accessionNumber"][k], "form": b["form"][k],
                             "filing_date": b["filingDate"][k], "acceptance": b["acceptanceDateTime"][k],
                             "report_date": b["reportDate"][k]})
    df = pd.DataFrame(rows).drop_duplicates("accession")
    df = df[df["filing_date"] <= end]
    return df.sort_values(["filing_date", "acceptance", "accession"]).reset_index(drop=True)


def filing_path(raw: Path, accession: str, cik: int = CIK) -> Path:
    return Path(raw) / f"{cik}_{accession.replace('-', '')}.txt"


def fetch_filing(raw: Path, accession: str, cik: int = CIK) -> str:
    """The complete submission text file (every document of the filing), cached."""
    body = sec_get(ARCHIVE.format(cik=cik, acc=accession), filing_path(raw, accession, cik))
    return body.decode("latin-1")


# ======================================================================== cover page

_NUM = r"\$?\s*([\d,]+)"


def _int(s: str) -> int:
    return int(s.replace(",", "").replace("$", "").strip())


def parse_cover(text: str) -> dict:
    """Report period, amendment type ('restatement' / 'new_holdings' / None), the table's stated entry and value
    totals (value as printed: thousands of dollars before 2023, dollars after) from the XML primary document when
    present, else from the plain-text cover and summary pages (the first ones in the file: the filer's own)."""
    out = {"amendment_type": None, "entry_total": None, "value_total": None, "is_xml": False}
    m = re.search(r"<edgarSubmission.*?</edgarSubmission>", text, re.S)
    if m:
        out["is_xml"] = True
        x = m.group(0)
        at = re.search(r"<amendmentType>\s*([^<]+?)\s*</amendmentType>", x)
        if at:
            out["amendment_type"] = "restatement" if "RESTATE" in at.group(1).upper() else "new_holdings"
        et = re.search(r"<tableEntryTotal>\s*([\d,]+)\s*</tableEntryTotal>", x)
        vt = re.search(r"<tableValueTotal>\s*([\d,]+)\s*</tableValueTotal>", x)
        out["entry_total"] = _int(et.group(1)) if et else None
        out["value_total"] = _int(vt.group(1)) if vt else None
        return out
    t = text
    # the first "This Amendment" block: which of the two boxes is ticked
    am = re.search(r"This\s+Amendment.*?\[[\s_]*([xX]?)[\s_]*\]\s*is\s+a\s+restatement.*?\[[\s_]*([xX]?)[\s_]*\]\s*"
                   r"adds\s+new", t, re.S | re.I)
    if am:
        if am.group(1):
            out["amendment_type"] = "restatement"
        elif am.group(2):
            out["amendment_type"] = "new_holdings"
    et = re.search(r"Information\s+Table\s+Entry\s+Total:?\s*" + _NUM, t, re.I)
    vt = re.search(r"Information\s+Table\s+Value\s+Total:?\s*" + _NUM, t, re.I)
    out["entry_total"] = _int(et.group(1)) if et else None
    out["value_total"] = _int(vt.group(1)) if vt else None
    return out


# ======================================================================== information tables

HOLDING_COLS = ["cusip", "name", "title", "value", "shares", "sh_prn", "put_call"]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_xml_tables(text: str) -> pd.DataFrame:
    """Rows of every <informationTable> XML document in the submission text."""
    rows = []
    for m in re.finditer(r"<(?:\w+:)?informationTable\b.*?</(?:\w+:)?informationTable>", text, re.S):
        root = ET.fromstring(m.group(0))
        for it in root.iter():
            if _strip_ns(it.tag) != "infoTable":
                continue
            d = {}
            for c in it.iter():
                t = _strip_ns(c.tag)
                if t in ("nameOfIssuer", "titleOfClass", "cusip", "value", "putCall", "sshPrnamtType", "sshPrnamt"):
                    d[t] = (c.text or "").strip()
            rows.append({"cusip": d.get("cusip", "").upper(), "name": d.get("nameOfIssuer", ""),
                         "title": d.get("titleOfClass", ""), "value": float(d.get("value", "nan").replace(",", "")),
                         "shares": float(d.get("sshPrnamt", "nan").replace(",", "")),
                         "sh_prn": d.get("sshPrnamtType", "SH").upper() or "SH",
                         "put_call": d.get("putCall", "").upper()})
    return pd.DataFrame(rows, columns=HOLDING_COLS)


# a CUSIP, printed with or without the spaces of "025816 10 9" (case-insensitive: one filing prints "82028k 20 0";
# 6-7 leading characters: one prints Wellpoint as "949773V 10 7"; such typos are corrected by the study's CUSIP map)
_CUSIPS = tuple(re.compile(r"(?<![0-9A-Za-z])([0-9A-Za-z]{%d}) {0,2}([0-9A-Za-z]{2}) {0,2}([0-9A-Za-z])(?![0-9A-Za-z])" % k)
                for k in (6, 7))
_AMT = r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)"
_AFTER_CUSIP = re.compile(r"\s+" + _AMT + r"\s+" + _AMT + r"(?=\s|$)(.*)$")
_PAIR = re.compile(r"(?<![\d,])" + _AMT + r"\s+" + _AMT + r"(?=\s|$)(.*)$")
_HEADER = re.compile(r"CUSIP", re.I)
VALUE_COLUMN_TOLERANCE = 3     # characters: a continuation's value must end where the CUSIP row's value ended


def _flags(rest: str) -> tuple[str, str]:
    toks = rest.upper().split()
    sh = "PRN" if "PRN" in toks else "SH"
    pc = "PUT" if "PUT" in toks else ("CALL" if "CALL" in toks else "")
    return sh, pc


def parse_text_table(text: str) -> pd.DataFrame:
    """Rows of the plain-text information table (pre-XML filings), read as a fixed-width table.

    A row starts at a CUSIP (at least 4 digits among its characters) followed by the value and the share amount. A
    line without a CUSIP whose first pair of amounts has its value ending in the same column (within
    ``VALUE_COLUMN_TOLERANCE``) as the last CUSIP row's value, with no digits before it, is another row of that CUSIP
    (the same security held through another manager; the text before it may be the rest of a wrapped issuer name).
    Wrapped "other managers" lines ("14, 15   54,600") fail the column test. Everything before the first line naming
    a CUSIP column is skipped (the cover pages never do); GRAND TOTAL lines are skipped. Name / class are the text
    before the CUSIP plus the name-only lines just above it (for the hand review only)."""
    lines = [ln.expandtabs(8) for ln in text.splitlines()]
    start = next((i for i, ln in enumerate(lines) if _HEADER.search(ln)), None)
    if start is None:
        return pd.DataFrame(columns=HOLDING_COLS)
    rows = []
    cur, val_end, name_buf = None, None, []
    for ln in lines[start + 1:]:
        if re.search(r"GRAND\s+TOTAL|^\s*TOTAL\b", ln.upper()):
            name_buf = []
            continue
        hit = None
        for pat in _CUSIPS:            # the regular 6 + 2 + 1 layout first, the 7-character typo only if none
            for m in pat.finditer(ln):
                cus = "".join(m.groups()).upper()
                if sum(ch.isdigit() for ch in cus) < 4:
                    continue
                tail = _AFTER_CUSIP.match(ln[m.end():])
                if tail:
                    hit = (m, cus, tail)
                    break
            if hit:
                break
        if hit:
            m, cus, tail = hit
            head = ln[:m.start()].strip()
            parts = re.split(r"\s{2,}", head)
            name, title = (" ".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else (head, "")
            name = " ".join(name_buf + [name]).strip()
            sh, pc = _flags(tail.group(3))
            cur = {"cusip": cus, "name": name, "title": title}
            val_end = m.end() + tail.end(1)
            rows.append({**cur, "value": float(_int(tail.group(1))), "shares": float(_int(tail.group(2))),
                         "sh_prn": sh, "put_call": pc})
            name_buf = []
            continue
        c = _PAIR.search(ln)
        if (c and cur is not None and abs(c.end(1) - val_end) <= VALUE_COLUMN_TOLERANCE
                and not re.search(r"\d", ln[:c.start()])):
            sh, pc = _flags(c.group(3))
            rows.append({**cur, "value": float(_int(c.group(1))), "shares": float(_int(c.group(2))),
                         "sh_prn": sh, "put_call": pc})
            name_buf = []
            continue
        s = ln.strip()
        if not s:
            continue
        if re.search(r"\d", s) or s.startswith("<") or "---" in s or not re.search(r"[A-Za-z]", s):
            name_buf = []
        else:
            name_buf = (name_buf + [s])[-4:]
    return pd.DataFrame(rows, columns=HOLDING_COLS)


def parse_filing(text: str) -> tuple[dict, pd.DataFrame]:
    """(cover facts, holding rows) of one submission text; the XML table when the filing has one."""
    cover = parse_cover(text)
    h = parse_xml_tables(text) if cover["is_xml"] else parse_text_table(text)
    cover["rows_parsed"] = int(len(h))
    cover["value_parsed"] = float(h["value"].sum()) if len(h) else 0.0
    return cover, h


def check_totals(cover: dict) -> dict:
    """Whether the parsed rows match the table's own totals (rows exactly; value within 0.5% or $5 thousand)."""
    et, vt = cover.get("entry_total"), cover.get("value_total")
    rows_ok = et is None or et == cover["rows_parsed"]
    val_ok = vt is None or abs(cover["value_parsed"] - vt) <= max(5.0, 0.005 * vt)
    return {"rows_ok": bool(rows_ok), "value_ok": bool(val_ok)}


# ======================================================================== holdings and states

def equity_positions(h: pd.DataFrame) -> pd.DataFrame:
    """Common-stock positions of one table: options (PUT / CALL rows) and principal amounts (PRN, bonds) dropped,
    rows of one CUSIP summed. Columns cusip, name, title, value, shares, sorted by value (ties: CUSIP)."""
    h = h[(h["put_call"].fillna("") == "") & (h["sh_prn"].fillna("SH").str.upper() != "PRN")].copy()
    h["cusip"] = h["cusip"].str.upper()
    h = h[h["cusip"].str.fullmatch(r"[0-9A-Z]{9}") & (h["cusip"] != "000000000")]
    g = h.groupby("cusip").agg(value=("value", "sum"), shares=("shares", "sum"), name=("name", "first"),
                               title=("title", "first")).reset_index()
    g = g[g["value"] > 0]
    return g.sort_values(["value", "cusip"], ascending=[False, True]).reset_index(drop=True)


RESTATEMENT_MIN_VALUE_SHARE = 0.5


def holding_events(filings: pd.DataFrame, tables: dict, mode: str = "public") -> list[dict]:
    """The holding states in filing order (section 0 rule). ``filings``: filing_list rows with ``amendment_type``;
    ``tables``: accession -> raw holding rows. ``mode``:

    - 'public': an original 13F-HR for a report period later than every period seen starts a state; a 13F-HR/A for
      that same latest period changes it from its own filing date: "adds new holdings entries" (or an unreadable
      type) appends its rows; "is a restatement" replaces the rows, but only when its table holds at least
      ``RESTATEMENT_MIN_VALUE_SHARE`` of the state's value (Berkshire's 2000-2001 confidential releases tick
      "restatement" yet list only the released positions: they are treated as additions, a CUSIP they list that is
      already in the state replacing that CUSIP's rows instead of being added twice); an amendment without
      table rows (a cover-page-only restatement) changes nothing. Originals and amendments for older periods are
      stale and ignored.
    - 'originals': amendments ignored (sensitivity).

    Each event: {filing_date, accession, form, report_date, kind ('original' / 'amend_add' / 'amend_restate'),
    rows (raw rows of the state), positions (``equity_positions`` of the state)}."""
    events = []
    latest = None
    rows = None
    for f in filings.itertuples():
        rep = str(f.report_date)
        add = tables[f.accession]
        if f.form == "13F-HR":
            if latest is not None and rep <= latest:
                continue
            latest, rows, kind = rep, add.copy(), "original"
        else:
            if mode != "public" or latest is None or rep != latest or len(add) == 0:
                continue
            restate = getattr(f, "amendment_type", None) == "restatement"
            if restate and add["value"].sum() >= RESTATEMENT_MIN_VALUE_SHARE * rows["value"].sum():
                rows, kind = add.copy(), "amend_restate"
            elif restate:
                # a small "restatement" is an addition, but a name it restates replaces that name's rows (fixed
                # 2026-10-23 after the run: the 2000-09-06 amendment restating Shaw Communications alone was appended,
                # doubling Shaw's shares in the state)
                again = rows["cusip"].astype(str).str.upper().isin(set(add["cusip"].astype(str).str.upper()))
                rows, kind = pd.concat([rows[~again], add], ignore_index=True), "amend_add"
            else:
                rows, kind = pd.concat([rows, add], ignore_index=True), "amend_add"
        events.append({"filing_date": str(f.filing_date), "accession": f.accession, "form": f.form,
                       "report_date": rep, "kind": kind, "rows": rows, "positions": equity_positions(rows)})
    return events


def states_by_trade_session(events: list[dict], sessions: pd.DatetimeIndex) -> dict:
    """trade session -> the last event whose filing date maps to it (several filings on one day, or on days before
    the same session, collapse into the latest state); events after the last session are dropped."""
    out = {}
    for e in events:
        d = trade_session(e["filing_date"], sessions)
        if d is not None:
            out[d] = e
    return dict(sorted(out.items()))


def trade_session(filing_date: str, sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """The first session strictly after the filing date (the close at which a state is traded)."""
    k = sessions.searchsorted(pd.Timestamp(filing_date), side="right")
    return sessions[k] if k < len(sessions) else None


def coverage_of(positions: pd.DataFrame, priced: set) -> dict:
    """Share of the reported value (and of the names) whose CUSIP is priced."""
    tot = float(positions["value"].sum())
    ok = positions["cusip"].isin(priced)
    return {"names": int(len(positions)), "names_priced": int(ok.sum()),
            "value_share_priced": float(positions.loc[ok, "value"].sum() / tot) if tot > 0 else np.nan}


# ======================================================================== one call for the study

def load(raw: Path, end: str, fixes: dict, mode: str = "public") -> tuple[pd.DataFrame, dict, list[dict]]:
    """(filings with their cover facts and parse checks, accession -> raw rows with ``fixes`` applied to the CUSIPs,
    holding events in ``mode``) for every 13F-HR / 13F-HR/A filed up to ``end`` (fetched once, then cached)."""
    fl = filing_list(submissions(raw), end)
    tables, facts = {}, []
    for f in fl.itertuples():
        cover, h = parse_filing(fetch_filing(raw, f.accession))
        h = h.assign(cusip=h["cusip"].replace(fixes))
        tables[f.accession] = h
        facts.append({**{k: cover[k] for k in ("amendment_type", "entry_total", "value_total", "is_xml",
                                                "rows_parsed", "value_parsed")}, **check_totals(cover)})
    fl = pd.concat([fl, pd.DataFrame(facts, index=fl.index)], axis=1)
    return fl, tables, holding_events(fl, tables, mode)
