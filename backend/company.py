"""Company detail for the Desk's Fundamentals tab: about, annual statements, ratios, shareholding and documents.

Free sources only, each labelled on the page:
  * About, annual profit & loss / balance sheet / cash flow, dividend yield, book value, 52-week range: Yahoo Finance (consolidated where
    the company has subsidiaries; rupee crore). Yahoo carries the last 4-5 financial years, not ten.
  * Face value, BSE's own EPS / PE / PB / ROE / OPM / NPM: BSE's quote header.
  * Promoter and public holding, pledge, insider trades: NSE disclosures already stored by backend/disclosures.py.
  * Documents: results filings and annual reports listed by BSE.
Statements are fetched on demand and kept for three days in company_cache (setupdesk.db).
"""
import json
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

from . import db, market, quality

CR = 1e7
TTL = 3 * 86400
BSE_HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com", "Accept": "application/json"}
SCHEMA = "CREATE TABLE IF NOT EXISTS company_cache (sym TEXT PRIMARY KEY, body TEXT, fetched REAL)"

PL_ROWS = [  # label, Yahoo row names (first found), kind
    ("Sales", ["Total Revenue", "Operating Revenue"], "cr"),
    ("EBITDA", ["EBITDA", "Normalized EBITDA"], "cr"),
    ("Depreciation", ["Reconciled Depreciation", "Depreciation And Amortization In Income Statement"], "cr"),
    ("Operating profit (EBIT)", ["EBIT", "Operating Income"], "cr"),
    ("Interest", ["Interest Expense", "Interest Expense Non Operating"], "cr"),
    ("Profit before tax", ["Pretax Income"], "cr"),
    ("Tax", ["Tax Provision"], "cr"),
    ("Net profit", ["Net Income Common Stockholders", "Net Income"], "cr"),
    ("EPS (₹)", ["Diluted EPS", "Basic EPS"], "num"),
]
BS_ROWS = [
    ("Equity (shareholders' funds)", ["Stockholders Equity", "Common Stock Equity"]),
    ("Minority interest", ["Minority Interest"]),
    ("Borrowings", ["Total Debt"]),
    ("Other liabilities", None),
    ("Total liabilities + equity", ["Total Assets"]),
    ("Net block (fixed assets)", ["Net PPE"]),
    ("Investments", ["Long Term Equity Investment", "Investments And Advances", "Investmentin Financial Assets"]),
    ("Receivables", ["Accounts Receivable", "Receivables"]),
    ("Inventory", ["Inventory"]),
    ("Cash and equivalents", ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]),
    ("Current assets", ["Current Assets"]),
    ("Current liabilities", ["Current Liabilities"]),
    ("Total assets", ["Total Assets"]),
]
CF_ROWS = [
    ("Cash from operations", ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]),
    ("Cash from investing", ["Investing Cash Flow"]),
    ("Cash from financing", ["Financing Cash Flow"]),
    ("Net cash flow", ["Changes In Cash"]),
    ("Capital expenditure", ["Capital Expenditure"]),
    ("Free cash flow", ["Free Cash Flow"]),
    ("Dividends paid", ["Cash Dividends Paid"]),
    ("Net borrowing", ["Net Issuance Payments Of Debt"]),
]


def _f(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _pick(df, names, col):
    for n in names or []:
        if n in df.index:
            v = _f(df.loc[n, col])
            if v is not None:
                return v
    return None


def _table(df, rows, scale=CR):
    """DataFrame (rows x periods, newest first) -> {'periods': [...oldest..newest], 'rows': [[label, [values]]]}."""
    if df is None or df.empty:
        return None
    cols = sorted(df.columns, key=lambda c: pd.Timestamp(c))
    out = []
    for item in rows:
        label, names = item[0], item[1]
        kind = item[2] if len(item) > 2 else "cr"
        if names is None:
            continue
        vals = []
        for c in cols:
            v = _pick(df, names, c)
            vals.append(None if v is None else (round(v / scale, 2) if kind == "cr" else round(v, 2)))
        if any(v is not None for v in vals):
            out.append([label, vals])
    return {"periods": [pd.Timestamp(c).strftime("%Y-%m-%d") for c in cols], "rows": out}


def _other_liabilities(bs):
    """Total liabilities + equity less equity, minority interest and borrowings."""
    if bs is None or bs.empty:
        return None
    cols = sorted(bs.columns, key=lambda c: pd.Timestamp(c))
    vals = []
    for c in cols:
        ta, eq, mi, debt = (_pick(bs, n, c) for n in (["Total Assets"], ["Stockholders Equity", "Common Stock Equity"], ["Minority Interest"], ["Total Debt"]))
        vals.append(None if ta is None or eq is None else round((ta - eq - (mi or 0) - (debt or 0)) / CR, 2))
    return vals


def _bse_header(scrip):
    try:
        r = requests.get("https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w", params={"quotetype": "EQ", "scripcode": scrip}, headers=BSE_HEAD, timeout=25).json()
    except Exception:
        return {}
    num = lambda k: _f(r.get(k))
    return {"face_value": num("FaceVal"), "eps": num("EPS"), "pe": num("PE"), "pb": num("PB"), "roe": num("ROE"), "opm": num("OPM"), "npm": num("NPM")}


def _documents(scrip):
    docs = {"results": [], "annual": [], "other": []}
    try:
        r = requests.get("https://api.bseindia.com/BseIndiaAPI/api/AnnualReport_New/w", params={"scripcode": scrip}, headers=BSE_HEAD, timeout=25).json()
        seen = set()
        for x in r.get("Table") or []:
            url = x.get("PDFDownload")
            if url and url not in seen:
                seen.add(url)
                docs["annual"].append({"title": f"Annual report {x.get('Year')}", "date": (x.get("Fld_AuthoriseDate") or "")[:10], "url": url})
        docs["annual"] = docs["annual"][:10]
    except Exception:
        pass
    try:
        from .sources import bse
        for a in bse.result_announcements(scrip, date.today() - timedelta(days=800))[:14]:
            docs["results"].append({"title": (a.get("NEWSSUB") or "Results").strip().replace("\r\n", " ")[:120], "date": a["DT_TM"][:10],
                                    "url": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/" + a["ATTACHMENTNAME"]})
    except Exception:
        pass
    return docs


def _holding(sym):
    con = db.connect()
    hold = [{"q": r["qend"], "promoter": r["promoter_pct"], "public": r["public_pct"]}
            for r in con.execute("SELECT qend, promoter_pct, public_pct FROM holding WHERE sym=? ORDER BY qend", (sym,))][-10:]
    pl = con.execute("SELECT * FROM pledge WHERE sym=? ORDER BY asof DESC LIMIT 1", (sym,)).fetchone()
    since = (date.today() - timedelta(days=365)).isoformat()
    ins = con.execute("SELECT kind, SUM(val_cr) v FROM insider WHERE sym=? AND d>=? AND kind IN ('Buy','Sell') GROUP BY kind", (sym, since)).fetchall()
    net = {r["kind"]: r["v"] for r in ins}
    return {"history": hold, "pledge": None if not pl else {"asof": pl["asof"], "pct_of_promoter": pl["pledged_pct_of_promoter"], "pct_of_total": pl["pledged_pct_of_total"]},
            "insider_12m": {"bought": round(net.get("Buy", 0) or 0, 2), "sold": round(net.get("Sell", 0) or 0, 2)}}


def _quarters(sym):
    con = db.connect()
    rows = [dict(r) for r in con.execute("SELECT qend, sales, op, np, eps, basis, source FROM quarter WHERE sym=? AND flags='' AND sales IS NOT NULL AND op IS NOT NULL AND np IS NOT NULL "
                                         "ORDER BY qend DESC LIMIT 12", (sym,))]
    ver = quality.for_stock(con, sym, rows)
    for r in rows:
        r["ver"] = ver[r["qend"]]
    return rows[::-1]


def build(sym):
    con = db.connect()
    st = con.execute("SELECT name, sector, scrip, kind FROM stock WHERE sym=?", (sym,)).fetchone()
    mcon = market.connect()
    cl = mcon.execute("SELECT macro, sector, industry, basic, mcap, scrip FROM class WHERE sym=?", (sym,)).fetchone()
    scrip = (st["scrip"] if st and st["scrip"] else None) or (cl[5] if cl else None)
    tk = yf.Ticker(sym + ".NS")
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    inr = info.get("financialCurrency") in (None, "INR")
    out = {"sym": sym, "fetched": datetime.now().isoformat(timespec="seconds"), "currency_ok": inr, "sources": ["Yahoo Finance", "BSE", "NSE"]}
    out["about"] = {"summary": info.get("longBusinessSummary"), "website": info.get("website"), "employees": info.get("fullTimeEmployees"),
                    "city": info.get("city"), "state": info.get("state"),
                    "path": [x for x in (cl[0], cl[1], cl[2], cl[3]) if x] if cl else []}
    dy = _f(info.get("dividendYield"))
    out["ratios"] = {"market_cap_cr": cl[4] if cl and cl[4] else (round(info["marketCap"] / CR) if info.get("marketCap") else None),
                     "high": _f(info.get("fiftyTwoWeekHigh")), "low": _f(info.get("fiftyTwoWeekLow")),
                     "pe": _f(info.get("trailingPE")), "book_value": _f(info.get("bookValue")), "dividend_yield_pct": dy,
                     "debt_to_equity": _f(info.get("debtToEquity")), "payout_ratio_pct": None if _f(info.get("payoutRatio")) is None else round(info["payoutRatio"] * 100, 1),
                     "institutions_pct": None if _f(info.get("heldPercentInstitutions")) is None else round(info["heldPercentInstitutions"] * 100, 1),
                     **({} if not scrip else {"bse": _bse_header(scrip)})}
    if inr:
        for key, attr, rows in (("pl", "income_stmt", PL_ROWS), ("cf", "cashflow", CF_ROWS), ("bs", "balance_sheet", BS_ROWS)):
            try:
                df = getattr(tk, attr)
                t = _table(df, rows)
                if key == "bs" and t:
                    ol = _other_liabilities(df)
                    cols = sorted(df.columns, key=lambda c: pd.Timestamp(c))
                    if ol:
                        pos = [i for i, r in enumerate(t["rows"]) if r[0] == "Borrowings"]
                        t["rows"].insert((pos[0] + 1) if pos else len(t["rows"]), ["Other liabilities", ol])
                out[key] = t
            except Exception as e:
                out[key] = None
                out.setdefault("errors", []).append(f"{key}: {type(e).__name__}")
        try:                                                   # the last four reported quarters, for a trailing-twelve-month column
            q = tk.quarterly_income_stmt
            if q is not None and q.shape[1] >= 4:
                cols = sorted(q.columns, key=lambda c: pd.Timestamp(c))[-4:]
                span = (pd.Timestamp(cols[-1]) - pd.Timestamp(cols[0])).days
                if not 255 <= span <= 290:                              # Yahoo sometimes skips a quarter; four non-consecutive ones are not a year
                    raise ValueError('quarters not consecutive')
                ttm = {}
                for label, names, kind in PL_ROWS:
                    vs = [_pick(q, names, c) for c in cols]
                    if None not in vs:
                        ttm[label] = round(sum(vs) / (CR if kind == "cr" else 1), 2)
                out["ttm"] = {"through": pd.Timestamp(cols[-1]).strftime("%Y-%m-%d"), "values": ttm}
        except Exception:
            pass
    out["quarters"] = _quarters(sym)
    out["holding"] = _holding(sym)
    out["docs"] = _documents(scrip) if scrip else {"results": [], "annual": [], "other": []}
    return out


def get(sym, refresh=False):
    con = db.connect()
    con.execute(SCHEMA)
    row = con.execute("SELECT body, fetched FROM company_cache WHERE sym=?", (sym,)).fetchone()
    if row and not refresh and time.time() - row["fetched"] < TTL:
        d = json.loads(row["body"])
        d["quarters"] = _quarters(sym)                         # always from our own table
        d["holding"] = _holding(sym)
        return d
    d = build(sym)
    con.execute("INSERT OR REPLACE INTO company_cache VALUES(?,?,?)", (sym, json.dumps(d), time.time()))
    con.commit()
    return d


# ---------------------------------------------------------------- standalone figures (the Consolidated / Standalone switch)
SA_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarter_basis (sym TEXT, basis TEXT, qend TEXT, sales REAL, op REAL, np REAL, eps REAL, source TEXT, filed TEXT,
  PRIMARY KEY (sym, basis, qend));
CREATE TABLE IF NOT EXISTS basis_fetch (sym TEXT PRIMARY KEY, fetched REAL, has_both INTEGER, note TEXT);
"""


def _nse_standalone(sym, log=None):
    """Standalone quarters from NSE's XBRL filings (history to Dec 2024) and whether a consolidated filing exists too."""
    from .sources import nse
    s = requests.Session()
    s.headers.update(nse.HEADERS)
    s.get("https://www.nseindia.com/", timeout=20)
    filings = s.get(nse.API.format(sym=sym), timeout=30).json()
    bases = {f.get("consolidated") for f in filings}
    best = {}
    for f in filings:
        if f.get("consolidated") == "Consolidated" or not (f.get("xbrl") or "").startswith("http"):
            continue
        try:
            filed = datetime.strptime(f["filingDate"], "%d-%b-%Y %H:%M")
        except (ValueError, TypeError):
            filed = datetime.strptime(f["toDate"], "%d-%b-%Y")
        if f["toDate"] not in best or filed > best[f["toDate"]][0]:
            best[f["toDate"]] = (filed, f)
    out = {}
    for to_date, (filed, f) in sorted(best.items(), key=lambda kv: datetime.strptime(kv[0], "%d-%b-%Y"))[-16:]:
        try:
            q = nse.parse_xbrl(s.get(f["xbrl"], timeout=30).text)
        except requests.RequestException:
            continue
        time.sleep(0.2)
        if q:
            out[q["qend"]] = {**q, "source": "NSE XBRL", "filed": filed.strftime("%Y-%m-%d")}
    return out, ("Consolidated" in bases and len(bases) > 1)


def _bse_standalone(scrip, since):
    """Standalone (and a consolidated flag) from BSE result PDFs filed since `since`."""
    from concurrent.futures import ThreadPoolExecutor
    from .sources import bse, filing
    sess = requests.Session()
    ann = bse.result_announcements(scrip, since, session=sess)[:6]

    def one(a):
        try:
            pdf = bse.fetch_pdf(a["ATTACHMENTNAME"], requests.Session())
            return a, filing.quarters_by_basis(pdf, use_ocr=False, filed=a["DT_TM"]) if pdf else {}
        except Exception:
            return a, {}
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(one, ann))
    out, both = {}, False
    for a, by in sorted(res, key=lambda r: r[0]["DT_TM"]):                       # oldest first, so a newer filing overrides
        both = both or ("Standalone" in by and "Consolidated" in by)
        for q in by.get("Standalone", []):
            if not q["flags"] and None not in (q["sales"], q["op"], q["np"]):
                out[q["qend"]] = {"qend": q["qend"], "sales": q["sales"], "op": q["op"], "np": q["np"], "eps": q.get("eps"), "source": "BSE result PDF", "filed": a["DT_TM"][:10]}
    return out, both


def _annual(quarters):
    """Financial years (April-March) made of four reported standalone quarters."""
    by = {}
    for q in quarters:
        d = date.fromisoformat(q["qend"])
        fy = d.year if d.month <= 3 else d.year + 1
        by.setdefault(fy, {})[d.month] = q
    out = []
    for fy in sorted(by):
        qs = by[fy]
        if {6, 9, 12, 3} <= set(qs):
            vals = [qs[m] for m in (6, 9, 12, 3)]
            out.append({"fy": fy, "end": f"{fy}-03-31", "sales": round(sum(v["sales"] for v in vals), 1), "op": round(sum(v["op"] for v in vals), 1),
                        "np": round(sum(v["np"] for v in vals), 1), "eps": None if any(v.get("eps") is None for v in vals) else round(sum(v["eps"] for v in vals), 2)})
    return out


def standalone(sym, refresh=False):
    con = db.connect()
    con.executescript(SA_SCHEMA)
    row = con.execute("SELECT * FROM basis_fetch WHERE sym=?", (sym,)).fetchone()
    if not row or refresh or time.time() - row["fetched"] > TTL:
        st = con.execute("SELECT scrip FROM stock WHERE sym=?", (sym,)).fetchone()
        cl = market.connect().execute("SELECT scrip FROM class WHERE sym=?", (sym,)).fetchone()
        scrip = (st["scrip"] if st and st["scrip"] else None) or (cl[0] if cl else None)
        notes = []
        try:
            nse_q, nse_both = _nse_standalone(sym)
        except Exception as e:
            nse_q, nse_both = {}, False
            notes.append(f"NSE: {type(e).__name__}")
        bse_q, bse_both = {}, False
        if scrip:
            try:
                bse_q, bse_both = _bse_standalone(scrip, date(2025, 1, 1))
            except Exception as e:
                notes.append(f"BSE: {type(e).__name__}")
        merged = {**nse_q, **bse_q}                         # BSE filings are newer than NSE's feed; they win where both have a quarter
        con.execute("DELETE FROM quarter_basis WHERE sym=? AND basis='Standalone'", (sym,))
        for q in merged.values():
            con.execute("INSERT OR REPLACE INTO quarter_basis VALUES(?,?,?,?,?,?,?,?,?)", (sym, "Standalone", q["qend"], q["sales"], q["op"], q["np"], q.get("eps"), q["source"], q["filed"]))
        con.execute("INSERT OR REPLACE INTO basis_fetch VALUES(?,?,?,?)", (sym, time.time(), int(nse_both or bse_both), "; ".join(notes)))
        con.commit()
        row = con.execute("SELECT * FROM basis_fetch WHERE sym=?", (sym,)).fetchone()
    qs = [dict(r) for r in con.execute("SELECT qend, sales, op, np, eps, source, filed FROM quarter_basis WHERE sym=? AND basis='Standalone' ORDER BY qend", (sym,))]
    return {"sym": sym, "has_both": bool(row["has_both"]), "note": row["note"], "quarters": qs[-12:], "annual": _annual(qs)[-6:]}
