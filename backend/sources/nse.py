"""Quarterly results from NSE's official XBRL filings (rupees crore).

NSE's results API only reaches the December 2024 quarter, so this covers history;
newer quarters come from sources/bse.py.
"""
import re
import time
from datetime import datetime

import requests

CR = 1e7
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
API = "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={sym}&period=Quarterly"


def _num(tag, xml):
    m = re.search(r"<in-bse-fin:%s[^>]*contextRef=\"OneD\"[^>]*>([^<]*)<" % tag, xml)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def parse_xbrl(xml):
    """Return one quarter's figures from a filing, or None if it is not a quarter."""
    start = re.search(r"<in-bse-fin:DateOfStartOfReportingPeriod[^>]*contextRef=\"OneD\"[^>]*>([^<]*)<", xml)
    end = re.search(r"<in-bse-fin:DateOfEndOfReportingPeriod[^>]*contextRef=\"OneD\"[^>]*>([^<]*)<", xml)
    if not start or not end:
        return None
    days = (datetime.fromisoformat(end.group(1)) - datetime.fromisoformat(start.group(1))).days
    if not 80 <= days <= 100:
        return None
    sales = _num("RevenueFromOperations", xml)
    exp = _num("Expenses", xml)
    fin = _num("FinanceCosts", xml) or 0.0
    dep = _num("DepreciationDepletionAndAmortisationExpense", xml) or 0.0
    # Prefer the "attributable to owners" figure. Some filings report it as 0.00 by
    # mistake while total profit is non-zero; then use total less minority share.
    owners = _num("ProfitOrLossAttributableToOwnersOfParent", xml)
    total = _num("ProfitLossForPeriod", xml)
    nci = _num("ProfitOrLossAttributableToNonControllingInterests", xml) or 0.0
    np_ = owners
    if (owners is None or owners == 0) and total:
        np_ = total - nci
    if sales is None or exp is None or np_ is None:
        return None
    eps = _num("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", xml)
    return {
        "qend": end.group(1),
        "sales": round(sales / CR, 2),
        "op": round((sales - (exp - fin - dep)) / CR, 2),
        "np": round(np_ / CR, 2),
        "eps": eps,
    }


def scrip_code(xml):
    m = re.search(r"<in-bse-fin:ScripCode[^>]*>(\d+)<", xml)
    return m.group(1) if m else None


def fetch_quarters(sym, session=None):
    s = session or requests.Session()
    s.headers.update(HEADERS)
    s.get("https://www.nseindia.com/", timeout=20)
    filings = s.get(API.format(sym=sym), timeout=30).json()
    # latest filing wins per (quarter, basis); consolidated beats standalone
    best = {}
    for f in filings:
        if not f.get("xbrl") or not f["xbrl"].startswith("http"):
            continue
        key = (f["toDate"], f["consolidated"])
        try:
            filed = datetime.strptime(f["filingDate"], "%d-%b-%Y %H:%M")
        except (ValueError, TypeError):  # some old filings carry "-"
            filed = datetime.strptime(f["toDate"], "%d-%b-%Y")
        if key not in best or filed > best[key][0]:
            best[key] = (filed, f)
    out, scrip = {}, None
    for (to_date, basis), (filed, f) in sorted(best.items(), key=lambda kv: kv[1][0]):
        if basis != "Consolidated" and datetime.strptime(to_date, "%d-%b-%Y").strftime("%Y-%m-%d") in out:
            continue
        try:
            xml = s.get(f["xbrl"], timeout=30).text
            scrip = scrip or scrip_code(xml)
            q = parse_xbrl(xml)
        except requests.RequestException:
            continue
        time.sleep(0.3)
        if q is None:
            continue
        q.update(basis=basis, source="NSE XBRL", filed=filed.strftime("%Y-%m-%d"), ref=f["xbrl"])
        if basis == "Consolidated" or q["qend"] not in out:
            out[q["qend"]] = q
    return [out[k] for k in sorted(out)], scrip


ANNOUNCE = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={sym}&from_date={frm}&to_date={to}"
RESULT_DESC = re.compile(r"outcome of board meeting|financial result|integrated filing|board meeting", re.I)
NOT_RESULT = re.compile(r"newspaper|press release|intimation|schedule|analyst|transcript|presentation|trading window", re.I)


def result_announcements(sym, since, until=None, session=None):
    """Results-bearing PDFs from NSE's announcement feed -> [{'date', 'url'}], newest first.

    A second source for the same filings BSE lists: BSE's file store is missing some of them.
    """
    from datetime import date
    s = session or requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
                      "Accept": "application/json", "Referer": "https://www.nseindia.com/"})
    s.get("https://www.nseindia.com/", timeout=20)
    until = until or date.today()
    r = s.get(ANNOUNCE.format(sym=sym, frm=since.strftime("%d-%m-%Y"), to=until.strftime("%d-%m-%Y")), timeout=30)
    out = []
    for x in r.json():
        url = x.get("attchmntFile") or ""
        text = f'{x.get("desc", "")} {x.get("attchmntText", "")}'
        if url.lower().endswith(".pdf") and RESULT_DESC.search(x.get("desc", "")) and not NOT_RESULT.search(text[:120]):
            out.append({"date": datetime.strptime(x["an_dt"][:11], "%d-%b-%Y").strftime("%Y-%m-%d"), "url": url})
    return sorted(out, key=lambda a: a["date"], reverse=True)
