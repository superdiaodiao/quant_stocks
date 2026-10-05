"""Point-in-time fundamental factor zoo (pre-registered in docs/research_ledger_fundamentals.md).

40 financial-statement factors (value, profitability, growth, quality/accruals, investment, leverage/liquidity,
efficiency, size) from SEC XBRL companyfacts annual facts (10-K, usable only strictly after the filing date), plus
7 family composites and an all-family composite. Each is held as a monthly, long-only, equal-weight top-10 portfolio
in two universes (U300: the weekly top-300 dollar-volume list; UW: every eligible panel name with a point-in-time
market cap >= $300M, survivorship-biased, report only), $10k IBKR Tiered costs, signal at the last session of a month,
trade at the next session's close. Judged test: walk-forward selection (each January the factor / composite with the
best trailing-36-month Sharpe is held for the year), 2017-01..2026-08 vs ONEQ total return.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --check      # data pipeline checks (no returns)
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --register   # freeze the pre-registration hash
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --run        # run every configuration once
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_livermore as lv  # noqa: E402
from scripts import research_canslim_dev as cs  # noqa: E402
from scripts import research_megacap as mc  # noqa: E402
from scripts import research_indicators as ind  # noqa: E402
from scripts import research_reversal_dev as rev  # noqa: E402

from scripts import study_data_version as dv  # noqa: E402  (REVERSAL_DATA_VERSION; docs/robustness_data_v2.md)
OUT_V1 = ROOT / "output/research_only/fundamentals"
OUT = dv.versioned(OUT_V1)
LEDGER = ROOT / "docs/research_ledger_fundamentals.md"
FROZEN = OUT_V1 / "frozen_prereg.json"   # frozen rules are never versioned
FCACHE = dv.versioned(Path("/Users/bytedance/code/quant_stocks/research_cache/fundamentals"))
LISTED = lv.CACHE / "universe/weekly_listed.csv.gz"
SECFACTS = lv.CACHE / "prefilter/security_facts.csv.gz"
SUBMISSIONS = lv.CACHE / "raw/sec/submissions"

PRICE_START, PERF_START, END = "2011-06-01", "2014-01-01", "2026-08-31"
FIRST_SIGNAL = "2013-12-31"
PERIODS = {"H1 2014-2019": ("2014-01-01", "2019-12-31"),
           "H2 2020-2026-08": ("2020-01-01", "2026-08-31"),
           "Full 2014-2026-08": ("2014-01-01", "2026-08-31")}
JUDGED, FULL = ("H1 2014-2019", "H2 2020-2026-08"), "Full 2014-2026-08"
WF_PERIODS = {"WF 2017-2026-08": ("2017-01-01", "2026-08-31"), "WF 2017-2019": ("2017-01-01", "2019-12-31"),
              "WF 2020-2026-08": ("2020-01-01", "2026-08-31")}
WF_JUDGED = "WF 2017-2026-08"
WF_FIRST_YEAR, WF_LOOKBACK_MONTHS = 2017, 36
N_HOLD = 10
MAX_FY_AGE = 550
MAX_Q_AGE = 200
MIN_MCAP_WIDE = 3e8
MIN_PRICE_WIDE = 5.0
INT_COV_CAP = 1000.0
SMALL_MCAP_RATIO = 30.0
ACCOUNT = 10_000.0

# ======================================================================== concepts (section 1.3)

FLOW = {  # annual duration facts (USD unless noted)
    "rev": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet"),
    "cogs": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
             "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
    "gross": ("GrossProfit",),
    "oi": ("OperatingIncomeLoss",),
    "ni": ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"),
    "da": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
           "DepreciationAmortizationAndAccretionNet", "Depreciation"),
    "rd": ("ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    "int": ("InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating"),
    "pti": ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "div": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "buy": ("PaymentsForRepurchaseOfCommonStock",),
}
FLOW_PS = {"eps": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")}
FLOW_SH = {"sh": ("WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic")}
STOCK = {  # instants (USD)
    "ta": ("Assets",), "ca": ("AssetsCurrent",), "cl": ("LiabilitiesCurrent",), "tl": ("Liabilities",),
    "be": ("StockholdersEquity",), "be_nci": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             "Cash"),
    "debt_cur": ("DebtCurrent",), "ltd_cur": ("LongTermDebtCurrent",), "stb": ("ShortTermBorrowings",),
    "ltd_nc": ("LongTermDebtNoncurrent",), "ltd_all": ("LongTermDebt",),
    "re": ("RetainedEarningsAccumulatedDeficit",), "ar": ("AccountsReceivableNetCurrent",), "inv": ("InventoryNet",),
    "ap": ("AccountsPayableCurrent",), "tp": ("TaxesPayableCurrent", "AccruedIncomeTaxesCurrent"),
}
EPS_Q = cs.EPS_CONCEPTS
ANNUAL_FORMS = ("10-K",)          # 10-K, 10-K/A, 10-K405, 10-KT ...
FY0_GROUPS = ("ni", "rev", "oi", "cfo")

# ======================================================================== factors (section 1.4)

FAMILIES = {
    "value": ("ep", "bm", "sp", "cfp", "ebit_ev", "fcf_yield", "payout_yield"),
    "profitability": ("roe", "roa", "roic", "gpa", "opa", "gross_margin", "op_margin", "cbop"),
    "growth": ("sales_g1", "sales_g3", "eps_g1", "eps_g3", "q_eps_g", "opinc_g1"),
    "quality": ("accruals", "sloan", "cash_conv", "piotroski", "altman_z", "earn_var", "noa"),
    "investment": ("asset_g", "capex_assets", "share_iss"),
    "leverage": ("de", "nd_ebitda", "current_ratio", "int_cov"),
    "efficiency": ("asset_turn", "d_asset_turn", "rd_sales", "rd_mcap"),
    "size": ("size",),
}
SIGN = {f: 1 for fam in FAMILIES.values() for f in fam}
SIGN.update({f: -1 for f in ("accruals", "sloan", "earn_var", "noa", "asset_g", "capex_assets", "share_iss", "de",
                             "nd_ebitda", "size")})
FACTORS = tuple(f for fam in FAMILIES.values() for f in fam)
COMPOSITE_FAMILIES = tuple(k for k in FAMILIES if k != "size")
COMPOSITES = tuple(f"C_{k}" for k in COMPOSITE_FAMILIES) + ("C_ALL",)
RULES = FACTORS + COMPOSITES
UNIVERSES = ("U300", "UW")
assert len(FACTORS) == 40 and len(COMPOSITES) == 8
N_TESTS = len(RULES) * len(UNIVERSES)
N_WF = 4
MARKET_FACTORS = ("ep", "bm", "sp", "cfp", "ebit_ev", "fcf_yield", "payout_yield", "altman_z", "rd_mcap", "size")


# ======================================================================== companyfacts -> annual facts

def _payloads(cik: int) -> list:
    out = []
    for d in cs.CF_DIRS:
        p = d / f"CIK{int(cik):010d}.json.gz"
        if p.is_file():
            env = json.loads(gzip.decompress(p.read_bytes()))
            out.append(env.get("payload", env))
    return out


def facts_from_payload(cik: int, payload: dict) -> tuple[list, list]:
    """(annual rows, quarterly-EPS rows) of one companyfacts payload.
    annual rows: (cik, group, prio, kind, end, filed, val) with kind 'F' (annual duration) or 'S' (instant), 10-K forms.
    EPS rows follow ``research_canslim_dev.extract_eps_facts`` (every form, durations kept for eps_states)."""
    gaap = payload.get("facts", {}).get("us-gaap", {})
    rows, eps = [], []

    def scan(groups: dict, unit_want: str, kind: str):
        for group, concepts in groups.items():
            for prio, concept in enumerate(concepts):
                for unit, items in gaap.get(concept, {}).get("units", {}).items():
                    if unit != unit_want:
                        continue
                    for f in items:
                        if f.get("val") is None or not f.get("filed") or not f.get("end"):
                            continue
                        if not str(f.get("form", "")).startswith(ANNUAL_FORMS):
                            continue
                        if kind == "F":
                            if "start" not in f:
                                continue
                            dur = (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).days
                            if not 340 <= dur <= 390:
                                continue
                        elif "start" in f:
                            continue
                        rows.append((int(cik), group, prio, kind, f["end"], f["filed"], float(f["val"])))

    scan(FLOW, "USD", "F")
    scan(FLOW_PS, "USD/shares", "F")
    scan(FLOW_SH, "shares", "F")
    scan(STOCK, "USD", "S")
    for pri, concept in enumerate(EPS_Q):
        for unit, items in gaap.get(concept, {}).get("units", {}).items():
            if unit != "USD/shares":
                continue
            for f in items:
                if "start" not in f or f.get("val") is None:
                    continue
                eps.append((int(cik), concept, pri, f["start"], f["end"], float(f["val"]), f["filed"],
                            f.get("form", ""), f.get("accn", "")))
    return rows, eps


ANNUAL_COLS = ["cik", "group", "prio", "kind", "end", "filed", "val"]
EPS_COLS = ["cik", "concept", "priority", "start", "end", "val", "filed", "form", "accn"]


def extract_all(ciks, refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Annual facts and point-in-time quarterly EPS states for every CIK (all local companyfacts copies merged,
    duplicates dropped). Cached under research_cache/fundamentals/."""
    FCACHE.mkdir(parents=True, exist_ok=True)
    fa, fe, fd = FCACHE / "annual_facts.csv.gz", FCACHE / "eps_states.csv.gz", FCACHE / "annual_facts.ciks.json"
    want = sorted(c for c in set(int(c) for c in ciks) if cs._cf_path(c) is not None)
    if not refresh and fa.exists() and fe.exists() and fd.exists() and set(want) <= set(json.loads(fd.read_text())):
        return (pd.read_csv(fa, dtype={"end": str, "filed": str}),
                pd.read_csv(fe, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str}))
    ann, states = [], []
    for k, cik in enumerate(want):
        rows, eps = [], []
        for pl in _payloads(cik):
            r, e = facts_from_payload(cik, pl)
            rows += r
            eps += e
        if rows:
            ann.append(pd.DataFrame(rows, columns=ANNUAL_COLS).drop_duplicates(["group", "prio", "kind", "end",
                                                                                "filed", "val"]))
        if eps:
            ef = pd.DataFrame(eps, columns=EPS_COLS).drop_duplicates(["concept", "start", "end", "val", "filed"])
            st = cs.eps_states(ef)
            if len(st):
                states.append(st)
        if k % 250 == 0:
            print(f"  companyfacts: {k}/{len(want)} CIKs read", flush=True)
    a = pd.concat(ann, ignore_index=True)
    s = pd.concat(states, ignore_index=True)
    a.to_csv(fa, index=False, compression="gzip")
    s.to_csv(fe, index=False, compression="gzip")
    fd.write_text(json.dumps(want))
    (FCACHE / "factor_states.csv.gz").unlink(missing_ok=True)     # derived from the annual facts: rebuild
    return a, s


# ======================================================================== annual facts -> point-in-time states

@lru_cache(maxsize=None)
def _ord(d: str) -> int:
    return date.fromisoformat(str(d)[:10]).toordinal()


def _days(a, b) -> int:
    return _ord(b) - _ord(a)


def pick(known: dict, group: str, end: str):
    """Value of ``group`` for period ``end``: the first concept by priority among values already filed."""
    vals = known.get((group, end))
    if not vals:
        return None
    return vals[min(vals)]


def pick_stock(known: dict, group: str, fy_end: str, ends_by_group: dict):
    """Instant value of ``group`` dated within 7 days of ``fy_end`` (closest end wins)."""
    best = None
    for e in ends_by_group.get(group, ()):
        d = abs(_days(e, fy_end))
        if d <= 7 and (best is None or d < best[0]):
            best = (d, e)
    return None if best is None else pick(known, group, best[1])


def year_inputs(known: dict, fy_end: str, ends_by_group: dict) -> dict:
    """Raw inputs of one fiscal year (section 1.3 rules: first concept by priority; derived items)."""
    f = {g: pick(known, g, fy_end) for g in list(FLOW) + list(FLOW_PS) + list(FLOW_SH)}
    s = {g: pick_stock(known, g, fy_end, ends_by_group) for g in STOCK}
    x = {**f, **s}
    rev_ = x["rev"]
    gross = x["gross"]
    if gross is None and rev_ is not None and x["cogs"] is not None:
        gross = rev_ - x["cogs"]
    if gross is not None and rev_ is not None and gross > rev_ * 1.0001:
        gross = None
    x["gp"] = gross
    x["be_any"] = x["be"] if x["be"] is not None else x["be_nci"]
    if x["tl"] is None and x["ta"] is not None:
        eq = x["be_nci"] if x["be_nci"] is not None else x["be"]
        x["tl"] = None if eq is None else x["ta"] - eq
    if x["debt_cur"] is not None:
        std = x["debt_cur"]
    else:
        std = (x["ltd_cur"] or 0.0) + (x["stb"] or 0.0)
    x["std"] = std
    if x["ltd_nc"] is not None:
        x["ltd"] = x["ltd_nc"]
        x["debt"] = x["ltd_nc"] + std
    else:
        x["ltd"] = x["ltd_all"] or 0.0
        x["debt"] = (x["ltd_all"] or 0.0) + (x["stb"] or 0.0)
    x["cash0"] = x["cash"] or 0.0
    x["da0"] = x["da"] or 0.0
    x["ebit"] = x["oi"] if x["oi"] is not None else (
        x["pti"] + (x["int"] or 0.0) if x["pti"] is not None else None)
    return x


def _ratio(a, b, pos_den: bool = True):
    if a is None or b is None or not np.isfinite(a) or not np.isfinite(b):
        return np.nan
    if pos_den and b <= 0:
        return np.nan
    if b == 0:
        return np.nan
    return a / b


def _growth_abs(a, b):
    if a is None or b is None or b == 0:
        return np.nan
    return (a - b) / abs(b)


def accounting_factors(ys: list) -> dict:
    """Factor inputs that need no market data. ``ys`` = [FY0, FY-1, ..., FY-4] input dicts (None when unknown).
    Market factors are completed at the signal date (``market_factors``); this returns their accounting parts."""
    y0 = ys[0]
    y1 = ys[1] if len(ys) > 1 else None
    y3 = ys[3] if len(ys) > 3 else None
    g = lambda y, k: None if y is None else y.get(k)  # noqa: E731
    ta0, ta1 = y0["ta"], g(y1, "ta")
    avg_ta = (ta0 + ta1) / 2 if ta0 is not None and ta1 is not None else ta0
    out = {}
    # ---- market-factor numerators (completed later)
    out["ni0"], out["be0"], out["rev0"], out["cfo0"] = y0["ni"], y0["be_any"], y0["rev"], y0["cfo"]
    out["ebit0"], out["debt0"], out["cash0"], out["rd0"], out["tl0"] = y0["ebit"], y0["debt"], y0["cash0"], y0["rd"], y0["tl"]
    out["fcf0"] = None if y0["cfo"] is None else y0["cfo"] - (y0["capex"] or 0.0)
    out["payout0"] = (y0["div"] or 0.0) + (y0["buy"] or 0.0)
    # ---- profitability
    out["roe"] = _ratio(y0["ni"], y0["be_any"])
    out["roa"] = _ratio(y0["ni"], ta0)
    ic = None if y0["ebit"] is None or y0["be_any"] is None else y0["debt"] + y0["be_any"] - y0["cash0"]
    out["roic"] = _ratio(y0["ebit"], ic)
    out["gpa"] = _ratio(y0["gp"], ta0)
    out["opa"] = _ratio(y0["oi"], ta0)
    out["gross_margin"] = _ratio(y0["gp"], y0["rev"])
    om = _ratio(y0["oi"], y0["rev"])
    out["op_margin"] = om if not (np.isfinite(om) and om > 1.0) else np.nan    # OI above revenue: tagging error
    if y0["oi"] is not None and ta0 is not None and ta0 > 0:
        d = lambda k: (y0[k] - y1[k]) if (y1 is not None and y0[k] is not None and y1[k] is not None) else 0.0  # noqa
        out["cbop"] = (y0["oi"] + y0["da0"] + (y0["rd"] or 0.0) - d("ar") - d("inv") + d("ap")) / ta0
    else:
        out["cbop"] = np.nan
    # ---- growth
    r1 = g(y1, "rev")
    out["sales_g1"] = y0["rev"] / r1 - 1 if (y0["rev"] is not None and r1 is not None and r1 > 0) else np.nan
    r3 = g(y3, "rev")
    out["sales_g3"] = ((y0["rev"] / r3) ** (1 / 3) - 1 if (y0["rev"] is not None and r3 is not None and r3 > 0
                                                         and y0["rev"] > 0) else np.nan)
    out["eps_g1"] = _growth_abs(y0["eps"], g(y1, "eps"))
    out["eps_g3"] = _growth_abs(y0["eps"], g(y3, "eps"))
    out["opinc_g1"] = _growth_abs(y0["oi"], g(y1, "oi"))
    # ---- quality / accruals
    out["accruals"] = ((y0["ni"] - y0["cfo"]) / avg_ta if (y0["ni"] is not None and y0["cfo"] is not None and avg_ta
                                                          and avg_ta > 0) else np.nan)
    if (y1 is not None and None not in (y0["ca"], y1["ca"], y0["cl"], y1["cl"]) and avg_ta and avg_ta > 0):
        dca = y0["ca"] - y1["ca"]
        dcash = y0["cash0"] - y1["cash0"]
        dcl = y0["cl"] - y1["cl"]
        dstd = y0["std"] - y1["std"]
        dtp = (y0["tp"] or 0.0) - (y1["tp"] or 0.0)
        out["sloan"] = ((dca - dcash) - (dcl - dstd - dtp) - y0["da0"]) / avg_ta
    else:
        out["sloan"] = np.nan
    out["cash_conv"] = y0["cfo"] / y0["ni"] if (y0["cfo"] is not None and y0["ni"] is not None and y0["ni"] > 0) \
        else np.nan
    out["piotroski"] = piotroski(y0, y1)
    if None not in (y0["ca"], y0["cl"], y0["re"], y0["ebit"], y0["rev"], ta0) and ta0 > 0:
        out["altman_part"] = (1.2 * (y0["ca"] - y0["cl"]) / ta0 + 1.4 * y0["re"] / ta0 + 3.3 * y0["ebit"] / ta0
                              + 1.0 * y0["rev"] / ta0)
    else:
        out["altman_part"] = np.nan
    roas = [y["ni"] / y["ta"] for y in ys if y is not None and y["ni"] is not None and y["ta"] and y["ta"] > 0]
    out["earn_var"] = float(np.std(roas, ddof=1)) if len(roas) >= 3 else np.nan
    if ta1 is not None and ta1 > 0 and ta0 is not None and y0["be_any"] is not None:
        oa = ta0 - y0["cash0"]
        ol = ta0 - y0["debt"] - y0["be_any"]
        out["noa"] = (oa - ol) / ta1
    else:
        out["noa"] = np.nan
    # ---- investment
    out["asset_g"] = ta0 / ta1 - 1 if (ta0 is not None and ta1 is not None and ta1 > 0) else np.nan
    out["capex_assets"] = _ratio(y0["capex"], ta0)
    sh0, sh1 = y0["sh"], g(y1, "sh")
    out["share_iss"] = math.log(sh0 / sh1) if (sh0 and sh1 and sh0 > 0 and sh1 > 0) else np.nan
    # ---- leverage / liquidity
    out["de"] = _ratio(y0["debt"], y0["be_any"])
    ebitda = None if y0["ebit"] is None else y0["ebit"] + y0["da0"]
    out["nd_ebitda"] = _ratio(y0["debt"] - y0["cash0"], ebitda)
    out["current_ratio"] = _ratio(y0["ca"], y0["cl"])
    if y0["int"] is None or y0["ebit"] is None:
        out["int_cov"] = np.nan
    elif y0["int"] > 0:
        out["int_cov"] = min(y0["ebit"] / y0["int"], INT_COV_CAP)
    else:
        out["int_cov"] = INT_COV_CAP if y0["ebit"] > 0 else np.nan
    # ---- efficiency
    out["asset_turn"] = _ratio(y0["rev"], ta0)
    at1 = _ratio(g(y1, "rev"), ta1)
    out["d_asset_turn"] = out["asset_turn"] - at1 if np.isfinite(out["asset_turn"]) and np.isfinite(at1) else np.nan
    out["rd_sales"] = _ratio(y0["rd"], y0["rev"])
    return out


def piotroski(y0: dict, y1: dict | None) -> float:
    """Piotroski (2000) F-score; at least 7 of the 9 signals computable, scaled by 9 / computable."""
    sig = []
    roa0 = _ratio(y0["ni"], y0["ta"])
    sig.append(None if not np.isfinite(roa0) else roa0 > 0)
    sig.append(None if y0["cfo"] is None else y0["cfo"] > 0)
    roa1 = _ratio(y1["ni"], y1["ta"]) if y1 else np.nan
    sig.append(roa0 > roa1 if np.isfinite(roa0) and np.isfinite(roa1) else None)
    sig.append(y0["cfo"] > y0["ni"] if y0["cfo"] is not None and y0["ni"] is not None else None)
    if y1 and y0["ta"] and y1["ta"] and y0["ta"] > 0 and y1["ta"] > 0:
        l0, l1 = y0["ltd"] / y0["ta"], y1["ltd"] / y1["ta"]
        sig.append(l0 < l1 or (y0["ltd"] == 0 and y1["ltd"] == 0))
    else:
        sig.append(None)
    cr0 = _ratio(y0["ca"], y0["cl"])
    cr1 = _ratio(y1["ca"], y1["cl"]) if y1 else np.nan
    sig.append(cr0 > cr1 if np.isfinite(cr0) and np.isfinite(cr1) else None)
    sig.append(y0["sh"] <= y1["sh"] if y1 and y0["sh"] and y1["sh"] else None)
    gm0 = _ratio(y0["gp"], y0["rev"])
    gm1 = _ratio(y1["gp"], y1["rev"]) if y1 else np.nan
    sig.append(gm0 > gm1 if np.isfinite(gm0) and np.isfinite(gm1) else None)
    at0 = _ratio(y0["rev"], y0["ta"])
    at1 = _ratio(y1["rev"], y1["ta"]) if y1 else np.nan
    sig.append(at0 > at1 if np.isfinite(at0) and np.isfinite(at1) else None)
    known = [bool(s) for s in sig if s is not None]
    if len(known) < 7:
        return np.nan
    return sum(known) * 9.0 / len(known)


def fiscal_years(known: dict) -> list:
    """[FY0, FY-1, ..., FY-4] period ends: FY0 = latest annual end among FY0_GROUPS; FY-k = the end of an annual fact
    of those groups closest to FY0 - k years (within 20 days), else None."""
    ends = sorted({e for (g, e) in known if g in FY0_GROUPS})
    if not ends:
        return []
    fy0 = ends[-1]
    out = [fy0]
    t0 = _ord(fy0)
    for k in range(1, 5):
        target = t0 - round(365.25 * k)
        cand = [(abs(_ord(e) - target), e) for e in ends]
        best = min(cand)
        out.append(best[1] if best[0] <= 20 else None)
    return out


def company_states(facts: pd.DataFrame) -> pd.DataFrame:
    """One row per filing date: the accounting factors of the latest fiscal year known from facts filed up to that
    date (usable strictly later). For one (group, concept, period) the most recently filed value wins."""
    if facts.empty:
        return pd.DataFrame()
    known: dict = {}
    ends_by_group: dict = {}
    rows = []
    cik = int(facts["cik"].iloc[0])
    for filed, g in facts.sort_values("filed", kind="stable").groupby("filed", sort=True):
        for r in g.itertuples():
            known.setdefault((r.group, r.end), {})[r.prio] = r.val
            if r.kind == "S":
                ends_by_group.setdefault(r.group, set()).add(r.end)
        fys = fiscal_years(known)
        if not fys:
            continue
        ys = [year_inputs(known, e, ends_by_group) if e is not None else None for e in fys]
        row = {"cik": cik, "filed": filed, "fy0_end": fys[0]}
        row.update(accounting_factors(ys))
        rows.append(row)
    return pd.DataFrame(rows)


def build_states(annual: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    cache = FCACHE / "factor_states.csv.gz"
    if cache.exists() and not refresh:
        return pd.read_csv(cache, dtype={"filed": str, "fy0_end": str})
    parts = []
    for k, (cik, g) in enumerate(annual.groupby("cik")):
        st = company_states(g)
        if len(st):
            parts.append(st)
        if k % 500 == 0:
            print(f"  states: {k} CIKs", flush=True)
    out = pd.concat(parts, ignore_index=True)
    out.to_csv(cache, index=False, compression="gzip")
    return out


def asof_states(rows: pd.DataFrame, states: pd.DataFrame, eps: pd.DataFrame) -> pd.DataFrame:
    """For each (s, cik) row: the latest factor state filed strictly before s (dropped when FY0 ended more than
    MAX_FY_AGE days before s) and the latest quarterly-EPS state with avail strictly before s (q_end within
    MAX_Q_AGE days). Asserted point in time."""
    left = rows.copy()
    left["cik_i"] = pd.to_numeric(left["cik"], errors="coerce")
    left = left[left["cik_i"].notna()].copy()
    left["cik_i"] = left["cik_i"].astype("int64")
    left["s"] = pd.to_datetime(left["s"]).astype("datetime64[ns]")
    left = left.sort_values("s")
    st = states.copy()
    st["usable_from"] = (pd.to_datetime(st["filed"]) + pd.Timedelta(days=1)).astype("datetime64[ns]")
    st["cik_i"] = st["cik"].astype("int64")
    st = st.drop(columns=["cik"]).sort_values("usable_from")
    m = pd.merge_asof(left, st, left_on="s", right_on="usable_from", by="cik_i", direction="backward")
    used = m["filed"].notna()
    if not (pd.to_datetime(m.loc[used, "filed"]) < m.loc[used, "s"]).all():
        raise cs.DateGuardError("annual fact used on or before its filing date")
    stale = ~((m["s"] - pd.to_datetime(m["fy0_end"])).dt.days <= MAX_FY_AGE)
    acc_cols = [c for c in st.columns if c not in ("usable_from", "cik_i", "filed", "fy0_end")]
    m.loc[stale, acc_cols] = np.nan
    e = eps[["cik", "avail", "q_end", "c_growth"]].copy()
    e = e[e["avail"].notna()]
    e["usable_from"] = (pd.to_datetime(e["avail"]) + pd.Timedelta(days=1)).astype("datetime64[ns]")
    e["cik_i"] = e["cik"].astype("int64")
    e = e.drop(columns=["cik"]).rename(columns={"avail": "eps_avail"}).sort_values("usable_from")
    m = m.sort_values("s")
    m = pd.merge_asof(m, e.rename(columns={"usable_from": "eps_usable_from"}), left_on="s",
                      right_on="eps_usable_from", by="cik_i", direction="backward")
    ok = m["eps_avail"].notna()
    if not (pd.to_datetime(m.loc[ok, "eps_avail"]) < m.loc[ok, "s"]).all():
        raise cs.DateGuardError("quarterly EPS used on or before its availability date")
    fresh = (m["s"] - pd.to_datetime(m["q_end"])).dt.days <= MAX_Q_AGE
    m["q_eps_g"] = m["c_growth"].where(fresh)
    return m


def market_factors(m: pd.DataFrame) -> pd.DataFrame:
    """Complete the market-based factors with the point-in-time market cap ``mcap`` (NaN when unknown)."""
    mcap = m["mcap"].where(m["mcap"] > 0)
    m["ep"] = m["ni0"] / mcap
    m["bm"] = (m["be0"] / mcap).where(m["be0"] > 0)
    m["sp"] = m["rev0"] / mcap
    m["cfp"] = m["cfo0"] / mcap
    ev = mcap + m["debt0"] - m["cash0"]
    m["ebit_ev"] = (m["ebit0"] / ev).where(ev > 0)
    m["fcf_yield"] = m["fcf0"] / mcap
    m["payout_yield"] = m["payout0"] / mcap
    m["altman_z"] = m["altman_part"] + 0.6 * (mcap / m["tl0"]).where(m["tl0"] > 0)
    m["rd_mcap"] = m["rd0"] / mcap
    m["size"] = np.log(mcap)
    for f in FACTORS:
        m[f] = pd.to_numeric(m[f], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return m


# ======================================================================== composites (section 1.4)

def zscores(panel: pd.DataFrame, cols) -> pd.DataFrame:
    """Per signal date: signed value -> cross-sectional percentile rank -> standardised (mean 0, sd 1)."""
    out = pd.DataFrame(index=panel.index)
    for f in cols:
        v = panel[f] * SIGN[f]
        r = v.groupby(panel["s"]).rank(pct=True)
        mu = r.groupby(panel["s"]).transform("mean")
        sd = r.groupby(panel["s"]).transform("std")
        out[f] = (r - mu) / sd
    return out


def composites(panel: pd.DataFrame) -> pd.DataFrame:
    z = zscores(panel, FACTORS)
    out = pd.DataFrame(index=panel.index)
    for fam in COMPOSITE_FAMILIES:
        cols = list(FAMILIES[fam])
        need = math.ceil(len(cols) / 2)
        n = z[cols].notna().sum(axis=1)
        out[f"C_{fam}"] = z[cols].mean(axis=1).where(n >= need)
    fams = [f"C_{k}" for k in COMPOSITE_FAMILIES]
    n = out[fams].notna().sum(axis=1)
    out["C_ALL"] = out[fams].mean(axis=1).where(n >= 4)
    return out


# ======================================================================== data: prices for every panel security

def load_all() -> lv.WinData:
    """``research_livermore.load_window`` with every panel security kept (not only the top-300 names)."""
    name = "fundamentals"
    lv.WINDOWS[name] = {"perf_start": PERF_START, "perf_end": END, "price_start": PRICE_START,
                        "universe_start": "2012-01-01", "judged_from": PERF_START}
    # keep every panel id: give load_window a universe frame listing all of them by wrapping read_csv once
    real_read = pd.read_csv

    def read_csv(path, *a, **k):
        df = real_read(path, *a, **k)
        if str(path).endswith("weekly_universe_top300.csv.gz"):
            ids = real_read(lv.CACHE / "universe/weekly_liquidity.csv.gz", usecols=["security_id"],
                            dtype=str)["security_id"].unique()
            extra = sorted(set(ids) - set(df["security_id"]))
            pad = pd.DataFrame({"security_id": extra, "week_end": "2012-01-06"})
            df = pd.concat([df, pad], ignore_index=True)
            df["_pad"] = df["security_id"].isin(extra) & df["ticker"].isna()
        return df

    lv.pd.read_csv = read_csv
    try:
        data = lv.load_window(name)
    finally:
        lv.pd.read_csv = real_read
    data.universe = data.universe[~data.universe["_pad"].astype(bool)].drop(columns=["_pad"])
    return data


def sic_map(top: pd.DataFrame) -> dict:
    """CIK -> current SIC from local SEC submissions files (used only where the top-300 file has no SIC)."""
    out = {}
    for c in sorted(set(int(x) for x in top)):
        p = SUBMISSIONS / f"CIK{c:010d}.json.gz"
        if not p.is_file():
            continue
        try:
            d = json.loads(gzip.decompress(p.read_bytes()))
            d = d.get("payload", d)
            if str(d.get("sic", "")).strip():
                out[c] = int(d["sic"])
        except Exception:  # noqa: BLE001
            continue
    return out


def candidate_rows(data: lv.WinData, signals: list) -> pd.DataFrame:
    """Every eligible panel security at each signal (latest week <= s): U300 rows from the top-300 file, plus
    eligible listed names from weekly_listed. Columns s, security_id, ticker, cik, dv50_rank, in300, sic."""
    uni = data.universe
    wl = pd.read_csv(LISTED, dtype=str, usecols=["week_end", "security_id", "ticker", "eligible", "dv50_rank"])
    wl = cs.truncate(wl, "week_end", "2012-01-01", END)
    wl = wl[(wl["eligible"] == "True") & wl["security_id"].isin(data.close.columns)].copy()
    wl["week_end"] = pd.to_datetime(wl["week_end"])
    wl["dv50_rank"] = pd.to_numeric(wl["dv50_rank"], errors="coerce")
    facts = pd.read_csv(SECFACTS, dtype=str, usecols=["security_id", "cik"]).set_index("security_id")["cik"]
    u3 = uni[uni["dv50_rank"].notna() & uni["security_id"].isin(data.close.columns)].copy()
    weeks3 = np.array(sorted(u3["week_end"].unique()), dtype="datetime64[ns]")
    weeksl = np.array(sorted(wl["week_end"].unique()), dtype="datetime64[ns]")
    by3 = {w: g for w, g in u3.groupby("week_end")}
    byl = {w: g for w, g in wl.groupby("week_end")}
    rows = []
    for s in signals:
        k3 = weeks3.searchsorted(np.datetime64(s), side="right") - 1
        kl = weeksl.searchsorted(np.datetime64(s), side="right") - 1
        a = by3[pd.Timestamp(weeks3[k3])][["security_id", "ticker", "cik", "dv50_rank", "sic", "multi_class_group"]] \
            .assign(in300=True) if k3 >= 0 else pd.DataFrame()
        b = byl[pd.Timestamp(weeksl[kl])][["security_id", "ticker", "dv50_rank"]].assign(in300=False) \
            if kl >= 0 else pd.DataFrame()
        b = b[~b["security_id"].isin(a["security_id"])] if len(a) else b
        g = pd.concat([a, b], ignore_index=True)
        g["s"] = s
        g["universe_week"] = pd.Timestamp(weeks3[k3]) if k3 >= 0 else pd.NaT
        rows.append(g)
    c = pd.concat(rows, ignore_index=True)
    c["cik"] = c["cik"].fillna(c["security_id"].map(facts)).fillna(c["security_id"].str.split(".").str[0])
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    px = mc._value_at(data.close, c["security_id"], c["s"])
    lr = pd.to_datetime(c["security_id"].map(data.last_row))
    c["close_s"] = px
    c = c[np.isfinite(px) & (lr.isna() | (lr >= c["s"])).to_numpy()].copy()
    cs.assert_window(c["universe_week"].dropna(), "2012-01-01", END, "universe weeks used")
    c["sic"] = pd.to_numeric(c["sic"], errors="coerce")
    miss = c["sic"].isna() & c["cik"].notna()
    smap = sic_map(c.loc[miss, "cik"].dropna().astype(int).unique())
    c.loc[miss, "sic"] = c.loc[miss, "cik"].map(lambda x: smap.get(int(x), np.nan) if pd.notna(x) else np.nan)
    return c.reset_index(drop=True)


def reject_small_mcaps(c: pd.DataFrame, factor: float = SMALL_MCAP_RATIO) -> pd.DataFrame:
    """Data rule 1.1a: a market cap whose market cap / 50-day dollar volume is below 1/``factor`` of that signal's
    median over the top-50 (by dv50 rank) U300 names is a share-unit error (shares filed in thousands): set empty."""
    c = c.copy()
    ratio = c["mcap"] / c["dv50"]
    top = c["in300"] & (c["dv50_rank"] <= 50) & np.isfinite(ratio)
    med = ratio[top].groupby(c.loc[top, "s"]).median()
    lim = c["s"].map(med) / factor
    odd = np.isfinite(ratio) & (ratio < lim)
    c.loc[odd, "mcap"] = np.nan
    c.loc[odd, "mcap_src"] = "rejected_too_small"
    return c


def universes(c: pd.DataFrame) -> dict:
    """U300 and UW (section 1.5), one row per company: non-financial; UW also needs mcap >= $300M and close >= $5."""
    fin = c["sic"].between(6000, 6999)
    out = {}
    u3 = c[c["in300"] & ~fin].copy()
    u3["company"] = u3["multi_class_group"].where(u3["multi_class_group"].notna() & (u3["multi_class_group"] != ""),
                                                  u3["cik"].astype(str))
    u3["company"] = u3["company"].where(u3["company"] != "<NA>", u3["security_id"])
    out["U300"] = u3.sort_values(["s", "company", "dv50_rank", "security_id"]).drop_duplicates(["s", "company"])
    uw = c[~fin & (c["mcap"] >= MIN_MCAP_WIDE) & (c["close_s"] >= MIN_PRICE_WIDE)].copy()
    uw["company"] = uw["cik"].astype(str).where(uw["cik"].notna(), uw["security_id"])
    uw["dvr"] = uw["dv50_rank"].fillna(1e9)
    out["UW"] = uw.sort_values(["s", "company", "dvr", "security_id"]).drop_duplicates(["s", "company"]) \
        .drop(columns=["dvr"])
    return {k: v.reset_index(drop=True) for k, v in out.items()}


# ======================================================================== targets, walk-forward

def top_targets(panel: pd.DataFrame, col: str, n: int = N_HOLD) -> dict:
    """signal -> [(sid, 1/n, dv50_rank, mcap, src)] for the n names with the best signed value (ties: security id).
    Signals with fewer than n rankable names are skipped (no rebalance)."""
    sign = SIGN.get(col, 1)
    out = {}
    v = panel[["s", "security_id", "dv50_rank", "mcap", col]].dropna(subset=[col])
    v = v.assign(key=v[col] * sign)
    for s, g in v.groupby("s"):
        if len(g) < n:
            continue
        g = g.sort_values(["key", "security_id"], ascending=[False, True]).head(n)
        out[s] = [(r.security_id, 1.0 / n, float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan,
                   float(r.mcap) if pd.notna(r.mcap) else np.nan, "") for r in g.itertuples()]
    return out


def monthly_returns(nav: pd.Series, base: float = ACCOUNT) -> pd.Series:
    """Month-end-to-month-end returns of a daily NAV (month label = calendar month); the first month is measured
    from the starting capital ``base``."""
    me = nav.groupby(nav.index.to_period("M")).last()
    prev = me.shift(1)
    prev.iloc[0] = base
    return me / prev - 1


def trailing_sharpe(nav: pd.Series, asof: pd.Timestamp, months: int = WF_LOOKBACK_MONTHS) -> float:
    """Sharpe (mean / sd x sqrt 12) of the ``months`` monthly returns ending with the month of ``asof``, using
    only NAV values dated on or before ``asof``; NaN without a full window."""
    v = nav[nav.index <= asof]
    if len(v) == 0:
        return np.nan
    r = monthly_returns(v)
    last = pd.Timestamp(asof).to_period("M")
    r = r[r.index <= last].tail(months)
    if len(r) < months or r.index[-1] != last or r.std(ddof=1) == 0:
        return np.nan
    return float(r.mean() / r.std(ddof=1) * math.sqrt(12))


def walk_forward(navs: dict, targets: dict, signals: list, first_year: int = WF_FIRST_YEAR) -> tuple[dict, list]:
    """Each January Y: at the last signal of December Y-1 pick the candidate with the best trailing Sharpe
    (ties: name) and use its targets for that signal and the next 11. Returns (targets, picks)."""
    sigs = sorted(signals)
    out, picks = {}, []
    dec = [s for s in sigs if s.month == 12 and s.year >= first_year - 1]
    for s0 in dec:
        sh = {k: trailing_sharpe(nav, s0) for k, nav in navs.items()}
        sh = {k: v for k, v in sh.items() if np.isfinite(v)}
        if not sh:
            continue
        best = sorted(sh.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        year_sigs = [s for s in sigs if s >= s0][:12]
        for s in year_sigs:
            if s in targets[best[0]]:
                out[s] = targets[best[0]][s]
        top3 = sorted(sh.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        picks.append({"year": s0.year + 1, "selected_at": str(s0.date()), "pick": best[0], "trailing_sharpe": best[1],
                      "runner_up": top3[1][0] if len(top3) > 1 else None,
                      "third": top3[2][0] if len(top3) > 2 else None, "n_candidates": len(sh)})
    return out, picks


# ======================================================================== statistics

def _betacf(a, b, x, itmax=300, eps=3e-14):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: int) -> float:
    """One-sided upper-tail p-value of Student's t."""
    if not np.isfinite(t):
        return np.nan
    x = df / (df + t * t)
    p = 0.5 * _betai(df / 2.0, 0.5, x)
    return p if t > 0 else 1.0 - p


def bh_reject(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg step-up: boolean mask of rejected hypotheses at FDR q."""
    p = np.asarray(p, float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    ok = ranked <= q * (np.arange(1, n + 1) / n)
    rej = np.zeros(n, bool)
    if ok.any():
        k = np.max(np.nonzero(ok)[0])
        rej[order[:k + 1]] = True
    return rej


def ic_series(panel: pd.DataFrame, col: str, fwd: pd.Series) -> pd.Series:
    """Monthly Spearman rank correlation of the signed value with the next-month total return."""
    v = panel[["s", col]].assign(r=fwd.to_numpy()).dropna()
    out = {}
    for s, g in v.groupby("s"):
        if len(g) < 20:
            continue
        a = (g[col] * SIGN.get(col, 1)).rank()
        b = g["r"].rank()
        out[s] = float(np.corrcoef(a, b)[0, 1])
    return pd.Series(out, dtype=float)


def criteria(per: dict) -> dict:
    return mc.criteria(per)


def wf_criteria(m: dict) -> dict:
    a = m["cagr"] > m["oneq_cagr"] and m["t_monthly_excess_vs_oneq"] >= 2.0
    b = m["dd_shallower_than_oneq_pp"] >= 10.0 and m["cagr"] >= m["oneq_cagr"] - 0.03
    return {"A_cagr_above_oneq_and_t_ge_2": bool(a), "B_dd_10pp_shallower_and_cagr_within_3pp": bool(b),
            "pass": bool(a or b)}


# ======================================================================== pre-registration hash

def prereg_block(text: str | None = None) -> str:
    t = text if text is not None else LEDGER.read_text()
    a, b = t.index("<!-- PREREG-BEGIN -->"), t.index("<!-- PREREG-END -->")
    return t[a:b]


def prereg_hash(text: str | None = None) -> str:
    return hashlib.sha256(prereg_block(text).encode()).hexdigest()


def register():
    OUT.mkdir(parents=True, exist_ok=True)
    if FROZEN.exists():
        raise SystemExit(f"already registered: {json.loads(FROZEN.read_text())}")
    FROZEN.write_text(json.dumps({"sha256": prereg_hash(), "registered_at": pd.Timestamp.now("UTC").isoformat()},
                                 indent=1) + "\n")
    print("registered", prereg_hash())


# ======================================================================== pipeline

def prepare():
    data = load_all()
    print("DATE GUARD:", data.guard["assertion"])
    end = data.spec["effective_end"]
    sigs = [s for s in mc.signal_sessions(data.sessions) if s >= pd.Timestamp(FIRST_SIGNAL)]
    cand = candidate_rows(data, sigs)
    ciks = sorted(cand["cik"].dropna().astype(int).unique())
    alias = mc.successor_ciks(data.universe)
    sh = mc.extract_share_facts(sorted(set(ciks) | set(alias)), cache=FCACHE / "sec_share_facts.csv.gz")
    sh = mc.add_predecessor_facts(sh, alias)
    lists = mc.load_company_lists()
    dv50 = (data.close_adj * data.vol_adj).rolling(50, min_periods=20).median()
    saved = mc.SHARES_VS_FLOAT
    mc.SHARES_VS_FLOAT = np.inf      # data rule 1.1a: shares x price is not replaced by the public float here
    try:
        capped = mc.market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]]
                                .assign(dv50_rank=cand["dv50_rank"].where(cand["in300"])), sh, lists, data.close,
                                data.sig_idx, dv50)
    finally:
        mc.SHARES_VS_FLOAT = saved
    cand["mcap"] = capped["mcap"].to_numpy()
    cand["mcap_src"] = capped["mcap_src"].to_numpy()
    bad = cand["mcap_src"].astype(str).str.startswith("dollar_volume")
    cand.loc[bad, "mcap"] = np.nan
    cand["dv50"] = mc._value_at(dv50, cand["security_id"], cand["s"])
    cand = reject_small_mcaps(cand)
    annual, eps = extract_all(ciks)
    annual = annual[annual["filed"].astype(str) <= end]
    eps = eps[eps["avail"].astype(str) <= end]
    states = build_states(annual)
    states = states[states["filed"].astype(str) <= end]
    cs.assert_window(states["filed"], None, end, "fundamental filings used")
    unis = universes(cand)
    panels = {}
    for u, rows in unis.items():
        m = asof_states(rows, states, eps)
        m = market_factors(m)
        m = m.sort_values(["s", "security_id"]).reset_index(drop=True)
        comp = composites(m)
        panels[u] = pd.concat([m, comp], axis=1)
    oneq_lvl, oneq_px = ind.oneq_on_sessions(data.sessions, PRICE_START, end)
    return data, sigs, cand, panels, oneq_lvl, oneq_px


def coverage(panels: dict) -> pd.DataFrame:
    rows = []
    for u, p in panels.items():
        for y, g in p.groupby(p["s"].dt.year):
            r = {"universe": u, "year": int(y), "avg_names": round(len(g) / g["s"].nunique(), 1)}
            for f in RULES:
                r[f] = round(float(g[f].notna().mean()), 3)
            rows.append(r)
    return pd.DataFrame(rows)


def spot_check(panels: dict) -> pd.DataFrame:
    """A few well-known companies' factor values at December signals (eyeball against public statements)."""
    p = panels["U300"]
    keep = p[p["ticker"].isin(["AAPL", "MSFT", "INTC", "COST", "AMGN", "CSCO"]) & (p["s"].dt.month == 12)]
    cols = ["s", "ticker", "fy0_end", "filed", "mcap"] + list(FACTORS)
    return keep[cols].sort_values(["ticker", "s"])


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, cand, panels, *_ = prepare()
    cov = coverage(panels)
    cov.to_csv(OUT / "coverage.csv", index=False)
    sc = spot_check(panels)
    sc.to_csv(OUT / "spot_check.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov[["universe", "year", "avg_names", "ep", "bm", "gpa", "roic", "sales_g3", "q_eps_g", "piotroski",
               "altman_z", "sloan", "share_iss", "int_cov", "rd_sales", "size", "C_ALL"]].to_string())
    print(sc[sc["s"].dt.year.isin([2016, 2019, 2023])][["s", "ticker", "fy0_end", "roe", "roa", "gpa", "gross_margin",
                                                          "ep", "bm", "sales_g1", "eps_g1", "piotroski",
                                                          "altman_z", "de", "share_iss"]].round(3).to_string())


def run(args):
    if not FROZEN.exists():
        raise SystemExit("run --register first")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, cand, panels, oneq_lvl, oneq_px = prepare()
    coverage(panels).to_csv(OUT / "coverage.csv", index=False)
    summary, by_year, holds, results = [], [], [], {}
    navs, nav_out, targets_all = {}, {}, {}
    qqq_ref = None
    for u in UNIVERSES:
        p = panels[u]
        for rule in RULES:
            tg = top_targets(p, rule)
            targets_all[(u, rule)] = tg
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                              data.qqq_close)
            dates = sim["nav"].index
            oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
            qqq = mc.buy_hold(data.qqq_perf_idx, data.qqq_close, dates, mc.QQQ_HS)
            per = {}
            for pname, (a, b) in PERIODS.items():
                m = mc.window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
                per[pname] = m
                summary.append({"universe": u, "rule": rule, "family": family_of(rule), "period": pname, **m})
            crit = criteria(per)
            months = per[FULL]["months"]
            t = per[FULL]["t_monthly_excess_vs_oneq"]
            results[f"{u}:{rule}"] = {"universe": u, "rule": rule, "family": family_of(rule), "criteria": crit,
                                      "start": str(sim["start"].date()), "t_full": t, "months": months,
                                      "p_one_sided": t_sf(t, months - 1), "avg_names_held": float(sim["names"].mean()),
                                      "signals_skipped": int(len([s for s in sigs if s not in tg]))}
            navs[(u, rule)] = sim["nav"]
            nav_out[f"{u}:{rule}"] = sim["nav"]
            r = sim["nav"].pct_change().dropna()
            yo, yq = mc._yearly(oneq.pct_change().dropna()), mc._yearly(qqq.pct_change().dropna())
            for y, v in mc._yearly(r).items():
                by_year.append({"universe": u, "rule": rule, "year": y, "strategy": v, "oneq": yo.get(y),
                                "qqq": yq.get(y)})
            for s, lst in tg.items():
                for k, (sid, w, rk, mcap_, _) in enumerate(lst, 1):
                    holds.append({"universe": u, "rule": rule, "signal": s.date().isoformat(), "slot": k,
                                  "security_id": sid})
            if qqq_ref is None:
                nav_out["ONEQ"], nav_out["QQQ"] = oneq, qqq
                qper = {pn: mc.window_metrics(qqq, oneq, qqq, a, b) for pn, (a, b) in PERIODS.items()}
                for pn, m in qper.items():
                    summary.append({"universe": "-", "rule": "QQQ buy-hold", "family": "benchmark", "period": pn, **m})
                    summary.append({"universe": "-", "rule": "ONEQ buy-hold", "family": "benchmark", "period": pn,
                                    **mc.window_metrics(oneq, oneq, qqq, *PERIODS[pn])})
                qqq_ref = criteria(qper)
            print(f"{u:4s} {rule:14s} H1 {per[JUDGED[0]]['cagr']:+.1%} H2 {per[JUDGED[1]]['cagr']:+.1%} "
                  f"(ONEQ {per[JUDGED[0]]['oneq_cagr']:+.1%} / {per[JUDGED[1]]['oneq_cagr']:+.1%}) t {t:+.2f} "
                  f"pass {crit['pass']}", flush=True)
    # ---- multiple testing over the 96 tests
    res = pd.DataFrame(results.values())
    res["bonferroni_pass"] = res["p_one_sided"] < 0.05 / N_TESTS
    res["bh_pass"] = bh_reject(res["p_one_sided"].fillna(1).to_numpy(), 0.05)
    res3 = res[res["universe"] == "U300"].copy()
    res3["bonferroni_pass_u300"] = res3["p_one_sided"] < 0.05 / len(RULES)
    res3["bh_pass_u300"] = bh_reject(res3["p_one_sided"].fillna(1).to_numpy(), 0.05)
    res = res.merge(res3[["universe", "rule", "bonferroni_pass_u300", "bh_pass_u300"]], how="left",
                    on=["universe", "rule"])
    res["criterion_pass"] = res["criteria"].map(lambda c: c["pass"])
    mt = {"n_tests": N_TESTS, "bonferroni_t_one_sided_5pct_approx": NormalDist().inv_cdf(1 - 0.05 / N_TESTS),
          "nominal_t_ge_2": int((res["t_full"] >= 2).sum()),
          "expected_by_chance_t_ge_2": round(N_TESTS * (1 - NormalDist().cdf(2.0)), 1),
          "bonferroni_pass": int(res["bonferroni_pass"].sum()), "bh_fdr5_pass": int(res["bh_pass"].sum()),
          "criterion_pass": int(res["criterion_pass"].sum()),
          "u300": {"n_tests": len(RULES), "nominal_t_ge_2": int((res3["t_full"] >= 2).sum()),
                   "bonferroni_pass": int(res3["bonferroni_pass_u300"].sum()),
                   "bh_fdr5_pass": int(res3["bh_pass_u300"].sum()),
                   "criterion_pass": int(res[(res["universe"] == "U300")]["criterion_pass"].sum())}}
    # ---- information coefficients (diagnostic)
    ic_rows = []
    for u in UNIVERSES:
        p = panels[u]
        nxt = {s: n for s, n in zip(sigs[:-1], sigs[1:])}
        s_next = p["s"].map(nxt)
        i0 = mc._value_at(data.sig_idx, p["security_id"], p["s"])
        i1 = mc._value_at(data.sig_idx, p["security_id"], s_next.fillna(p["s"]))
        fwd = pd.Series(np.where(s_next.notna(), i1 / i0 - 1, np.nan), index=p.index)
        for rule in RULES:
            ic = ic_series(p, rule, fwd)
            for pname, (a, b) in PERIODS.items():
                x = ic[(ic.index >= pd.Timestamp(a) - pd.Timedelta(days=5)) & (ic.index <= pd.Timestamp(b))]
                ic_rows.append({"universe": u, "rule": rule, "family": family_of(rule), "period": pname,
                                "months": len(x), "mean_ic": float(x.mean()) if len(x) else np.nan,
                                "t": float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if len(x) > 2 else np.nan,
                                "share_positive": float((x > 0).mean()) if len(x) else np.nan})
    ic_df = pd.DataFrame(ic_rows)
    # ---- walk-forward (judged)
    wf_res, wf_picks = {}, []
    for u in UNIVERSES:
        for sel, pool in (("WF-F", FACTORS), ("WF-C", COMPOSITES)):
            cand_navs = {r: navs[(u, r)] for r in pool}
            tg, picks = walk_forward(cand_navs, {r: targets_all[(u, r)] for r in pool}, sigs)
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                              data.qqq_close)
            dates = sim["nav"].index
            oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
            qqq = mc.buy_hold(data.qqq_perf_idx, data.qqq_close, dates, mc.QQQ_HS)
            per = {pn: mc.window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
                   for pn, (a, b) in WF_PERIODS.items()}
            for pn, m in per.items():
                summary.append({"universe": u, "rule": sel, "family": "walk_forward", "period": pn, **m})
            crit = wf_criteria(per[WF_JUDGED])
            key = f"{u}:{sel}"
            wf_res[key] = {"universe": u, "selector": sel, "criteria": crit, "start": str(sim["start"].date()),
                           "periods": per, "judged": u == "U300",
                           "p_one_sided": t_sf(per[WF_JUDGED]["t_monthly_excess_vs_oneq"],
                                               per[WF_JUDGED]["months"] - 1)}
            for pk in picks:
                wf_picks.append({"universe": u, "selector": sel, **pk})
            nav_out[key] = sim["nav"]
            r = sim["nav"].pct_change().dropna()
            yo, yq = mc._yearly(oneq.pct_change().dropna()), mc._yearly(qqq.pct_change().dropna())
            for y, v in mc._yearly(r).items():
                by_year.append({"universe": u, "rule": sel, "year": y, "strategy": v, "oneq": yo.get(y),
                                "qqq": yq.get(y)})
            m = per[WF_JUDGED]
            print(f"{key}: CAGR {m['cagr']:+.1%} ONEQ {m['oneq_cagr']:+.1%} t {m['t_monthly_excess_vs_oneq']:+.2f} "
                  f"DD {m['max_dd']:.0%} (ONEQ {m['oneq_max_dd']:.0%}) pass {crit['pass']}", flush=True)
    verdict = any(v["criteria"]["pass"] for v in wf_res.values() if v["judged"])
    # ---- write
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    res.drop(columns=["criteria"]).assign(**{
        "A": res["criteria"].map(lambda c: c["A_cagr_above_oneq_both_halves_and_full_t_ge_2"]),
        "B": res["criteria"].map(lambda c: c["B_dd_10pp_shallower_and_cagr_within_3pp_both_halves"])}) \
        .to_csv(OUT / "tests.csv", index=False)
    ic_df.to_csv(OUT / "ic.csv", index=False)
    pd.DataFrame(wf_picks).to_csv(OUT / "wf_picks.csv", index=False)
    pd.DataFrame(holds).to_csv(OUT / "holdings_by_signal.csv.gz", index=False, compression="gzip")
    pd.DataFrame(nav_out).to_csv(OUT / "nav_daily.csv.gz", compression="gzip")
    out = {"prereg_sha256": prereg_hash(), "guard": data.guard["assertion"], "n_tests": N_TESTS, "n_wf": N_WF,
           "multiple_testing": mt, "walk_forward": wf_res, "verdict_pass": verdict,
           "wf_bonferroni_t_2_trials": NormalDist().inv_cdf(1 - 0.05 / 2),
           "qqq_buyhold_reference_criteria": qqq_ref, "first_signal": str(sigs[0].date()),
           "last_signal": str(sigs[-1].date()), "n_signals": len(sigs),
           "terminal_events": int(len(data.terminal_events))}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=_json) + "\n")
    print(json.dumps(mt, indent=1))
    print("VERDICT (U300 walk-forward):", "PASS" if verdict else "FAIL")
    return out


def _json(x):
    if isinstance(x, (np.floating, float)):
        return None if not np.isfinite(x) else round(float(x), 6)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return str(x)


def family_of(rule: str) -> str:
    if rule.startswith("C_"):
        return "composite"
    return next(k for k, v in FAMILIES.items() if rule in v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    a = ap.parse_args(argv)
    if a.register:
        register()
    elif a.check:
        check(a)
    else:
        run(a)


if __name__ == "__main__":
    main()
