"""Annual results from NSE's filings, back to the March 2017 year (rupee crore).

NSE's annual results list (period=Annual) holds one filing per financial year and basis, for twenty years. What is machine-readable depends on the year:
  FY2018 onward        XBRL: the full-year profit and loss; reserves and capital; the cash-flow statement from FY2022; the balance sheet from FY2023
  FY2017 and earlier   an HTML page of the old results format: only the headline lines are trustworthy (sales, profit before tax, tax, profit, EPS)
The XBRL files label each figure with a context: FourD is the financial year, OneI the year-end balance sheet.
"""
import html
import re
import time
from datetime import datetime

import requests

CR = 1e7
LAKH = 1e2
HEAD = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
API = "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={sym}&period=Annual"


def _tag(xml, tag, ctx):
    m = re.search(r"<in-bse-fin:%s[^>]*contextRef=\"%s\"[^>]*>([^<]*)<" % (tag, ctx), xml)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _cr(v):
    return None if v is None else round(v / CR, 2)


def parse_xbrl(xml):
    """One financial year from a Ind-AS annual filing: {sales, other, op, dep, fin, pbt, tax, np, eps, paid_up, reserves, ...}, or None."""
    s = lambda t: _tag(xml, t, "FourD")
    i = lambda t: _tag(xml, t, "OneI")
    sales, exp = s("RevenueFromOperations"), s("Expenses")
    start = re.search(r"<in-bse-fin:DateOfStartOfReportingPeriod[^>]*contextRef=\"FourD\"[^>]*>([^<]*)<", xml)
    end = re.search(r"<in-bse-fin:DateOfEndOfReportingPeriod[^>]*contextRef=\"FourD\"[^>]*>([^<]*)<", xml)
    if sales is None or exp is None or not start or not end:
        return None
    if not 330 <= (datetime.fromisoformat(end.group(1)) - datetime.fromisoformat(start.group(1))).days <= 380:
        return None
    fin, dep = s("FinanceCosts") or 0.0, s("DepreciationDepletionAndAmortisationExpense") or 0.0
    owners, total = s("ProfitOrLossAttributableToOwnersOfParent"), s("ProfitLossForPeriod")
    nci = s("ProfitOrLossAttributableToNonControllingInterests") or 0.0
    np_ = owners if owners not in (None, 0) else (None if total is None else total - nci)
    pbt = s("ProfitBeforeTax")
    row = {"end": end.group(1), "sales": _cr(sales), "other": _cr(s("OtherIncome")), "op": _cr(sales - (exp - fin - dep)), "dep": _cr(dep), "fin": _cr(fin),
           "pbt": _cr(pbt), "tax": None if pbt is None or total is None else _cr(pbt - total), "np": _cr(np_),
           "eps": s("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"),
           "paid_up": _cr(i("EquityShareCapital") if i("EquityShareCapital") is not None else s("PaidUpValueOfEquityShareCapital")),
           "reserves": _cr(i("OtherEquity") if i("OtherEquity") is not None else s("ReserveExcludingRevaluationReserves"))}
    # balance sheet (filed from FY2023) and cash flow (from FY2022)
    if i("Assets") is not None:
        eq = i("EquityAttributableToOwnersOfParent")
        if eq is None and row["paid_up"] is not None and row["reserves"] is not None:
            eq = (row["paid_up"] + row["reserves"]) * CR
        borrow = (i("BorrowingsNoncurrent") or 0.0) + (i("BorrowingsCurrent") or 0.0)
        fixed = sum(i(t) or 0.0 for t in ("PropertyPlantAndEquipment", "CapitalWorkInProgress", "IntangibleAssetsUnderDevelopment", "OtherIntangibleAssets", "Goodwill"))
        row.update({"equity": _cr(eq), "minority": _cr(i("NonControllingInterest")), "borrowings": _cr(borrow), "assets": _cr(i("Assets")),
                    "fixed": _cr(fixed), "investments": _cr((i("CurrentInvestments") or 0.0) + (i("NoncurrentInvestments") or 0.0)),
                    "receivables": _cr(i("TradeReceivablesCurrent")), "inventory": _cr(i("Inventories")), "cash": _cr(i("CashAndCashEquivalents")),
                    "cur_assets": _cr(i("CurrentAssets")), "cur_liab": _cr(i("CurrentLiabilities"))})
    cfo = s("CashFlowsFromUsedInOperatingActivities")
    if cfo is not None:
        capex = sum(s(t) or 0.0 for t in ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"))
        row.update({"cfo": _cr(cfo), "cfi": _cr(s("CashFlowsFromUsedInInvestingActivities")), "cff": _cr(s("CashFlowsFromUsedInFinancingActivities")), "capex": _cr(capex)})
    return row


def _flat(text):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))


NUM = r"(-?\d[\d.]*)"


def parse_html(text):
    """The headline lines of an old-format annual result (rupees in lakhs): sales, profit before tax, tax, net profit and EPS, kept only when they add up."""
    t = _flat(text)
    if not re.search(r"in lakh", t, re.I):
        return None
    g = lambda pat: (lambda m: float(m.group(1)) if m else None)(re.search(pat, t, re.I))
    sales = g(r"Total income from operations \(net\)[^\d-]*" + NUM)
    if sales is None:
        sales = g(r"Net sales/income from operations[^\d-]*" + NUM)
    pbt = g(r"Profit / \(Loss\) from ordinary activities before tax\s+" + NUM)
    tax = g(r"Tax expense\s+" + NUM)
    np_ = g(r"(?:Consolidated Net Profit/Loss for the period|Net Profit / \(Loss\) after taxes, minority interest and share of profit / \(loss\) of associates)\s+" + NUM)
    if np_ is None:
        np_ = g(r"Net Profit / \(Loss\) for the period\s+" + NUM)
    eps = g(r"Basic EPS for continued and discontinued operations\s+" + NUM)
    if eps is None:
        eps = g(r"\(a\) Basic\s+" + NUM)
    if None in (sales, pbt, tax, np_) or sales <= 0:
        return None
    if abs(pbt - tax - np_) > max(0.01 * abs(pbt), 50):          # the page has to add up (50 lakh = 0.5 crore of slack for associates and minorities)
        return None
    c = lambda v: None if v is None else round(v / LAKH, 2)
    return {"sales": c(sales), "pbt": c(pbt), "tax": c(tax), "np": c(np_), "eps": eps, "op": None}


def fetch(sym, years=12, session=None):
    """-> {'Consolidated': {end: row}, 'Standalone': {end: row}} for the latest `years` financial years NSE lists."""
    s = session or requests.Session()
    s.headers.update(HEAD)
    s.get("https://www.nseindia.com/", timeout=20)
    filings = s.get(API.format(sym=sym), timeout=30).json()
    best = {}
    for f in filings:
        key = (f["toDate"], f["consolidated"])
        try:
            filed = datetime.strptime(f["filingDate"], "%d-%b-%Y %H:%M")
        except (ValueError, TypeError):
            filed = datetime.strptime(f["toDate"], "%d-%b-%Y")
        if key not in best or filed > best[key][0]:
            best[key] = (filed, f)
    ends = sorted({datetime.strptime(k[0], "%d-%b-%Y") for k in best}, reverse=True)[:years]
    keep = {e.strftime("%d-%b-%Y") for e in ends}
    out = {"Consolidated": {}, "Standalone": {}}
    for (to, basis), (filed, f) in sorted(best.items(), key=lambda kv: datetime.strptime(kv[0][0], "%d-%b-%Y")):
        if to not in keep:
            continue
        label = "Consolidated" if basis == "Consolidated" else "Standalone"
        row = None
        try:
            if (f.get("xbrl") or "").endswith(".xml"):
                row = parse_xbrl(s.get(f["xbrl"], timeout=30).text)
                src = "NSE XBRL"
            elif f.get("resultDetailedDataLink"):
                row = parse_html(s.get(f["resultDetailedDataLink"], timeout=30).text)
                if row:
                    row["end"] = datetime.strptime(to, "%d-%b-%Y").strftime("%Y-%m-%d")
                src = "NSE results page"
        except requests.RequestException:
            continue
        time.sleep(0.25)
        if row:
            row.update(source=src, ref=f.get("xbrl") if src == "NSE XBRL" else f.get("resultDetailedDataLink"), filed=filed.strftime("%Y-%m-%d"))
            out[label][row["end"]] = row
    return out
