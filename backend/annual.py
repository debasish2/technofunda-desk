"""Ten financial years for the Desk's "10 Years" tab, consolidated and standalone.

    python -m backend.annual TITAN       # build and print one stock

Where each year comes from (all free, all rupee crore):
  FY2013-FY2017   NSE's old-format results pages: sales, net profit and EPS only (the other lines on those pages do not add up)
  FY2018-FY2024   NSE's XBRL filings: the full profit and loss, reserves, cash flow (from FY2022) and balance sheet (from FY2023)
  FY2025 onward   the site's own quarterly table (four filed quarters added up) for sales, operating profit and profit; Yahoo Finance for the rest
Balance sheet and cash flow come from Yahoo Finance for the years it carries (the last four or five), because its definitions stay the same
from year to year; NSE's XBRL fills older years it does not have. A year that cannot be built is left out, not guessed.
"""
import json
import sys
import time
from datetime import date, datetime

from . import db
from .sources import nse_annual

TTL = 30 * 86400
SCHEMA = "CREATE TABLE IF NOT EXISTS annual_cache (sym TEXT PRIMARY KEY, body TEXT, fetched REAL)"
YEARS = 10

YPL = {"Sales": "sales", "Depreciation": "dep", "Interest": "fin", "Profit before tax": "pbt", "Tax": "tax", "Net profit": "np", "EPS (₹)": "eps"}
YBS = {"Equity (shareholders' funds)": "equity", "Minority interest": "minority", "Borrowings": "borrowings", "Total assets": "assets",
       "Net block (fixed assets)": "fixed", "Investments": "investments", "Receivables": "receivables", "Inventory": "inventory",
       "Cash and equivalents": "cash", "Current assets": "cur_assets", "Current liabilities": "cur_liab"}
YCF = {"Cash from operations": "cfo", "Cash from investing": "cfi", "Cash from financing": "cff", "Capital expenditure": "capex"}


def _yahoo(co):
    """{end: {field: value}} from company.get()'s Yahoo tables."""
    out = {}
    for key, mapping in (("pl", YPL), ("bs", YBS), ("cf", YCF)):
        t = (co or {}).get(key)
        if not t:
            continue
        for label, vals in t["rows"]:
            f = mapping.get(label)
            if not f:
                continue
            for end, v in zip(t["periods"], vals):
                if v is None:
                    continue
                out.setdefault(end, {})[f] = abs(v) if f == "capex" else v
    return out


def _quarter_sums(sym):
    """{FY end: {sales, op, np}} for years whose four quarters (Jun, Sep, Dec, Mar) are all in the site's table."""
    con = db.connect()
    rows = [dict(r) for r in con.execute("SELECT qend, sales, op, np FROM quarter WHERE sym=? AND flags='' AND sales IS NOT NULL AND op IS NOT NULL AND np IS NOT NULL", (sym,))]
    by = {}
    for r in rows:
        d = date.fromisoformat(r["qend"])
        fy = d.year if d.month <= 3 else d.year + 1
        by.setdefault(fy, {})[d.month] = r
    out = {}
    for fy, qs in by.items():
        if {6, 9, 12, 3} <= set(qs):
            v = [qs[m] for m in (6, 9, 12, 3)]
            out[f"{fy}-03-31"] = {k: round(sum(x[k] for x in v), 1) for k in ("sales", "op", "np")}
    return out


BS_FIELDS = ("equity", "minority", "borrowings", "assets", "fixed", "investments", "receivables", "inventory", "cash", "cur_assets", "cur_liab", "cfo", "cfi", "cff", "capex")


def _merge(nse, yahoo, qsum, standalone_q=None):
    ends = sorted(set(nse) | set(yahoo) | set(qsum or {}) | set(standalone_q or {}))
    rows = []
    for end in ends:
        r = dict(nse.get(end) or {})
        r["end"] = end
        src = [r["source"]] if r.get("source") else []
        y = yahoo.get(end, {})
        if yahoo:
            for f in BS_FIELDS:                                     # Yahoo's balance sheet and cash flow, where it has the year
                if y.get(f) is not None:
                    r[f] = y[f]
                    if "Yahoo Finance" not in src:
                        src.append("Yahoo Finance")
        if r.get("sales") is None:
            q = (qsum or {}).get(end) or (standalone_q or {}).get(end)
            if q:
                r.update({k: q[k] for k in ("sales", "op", "np")})
                if q.get("eps") is not None:
                    r["eps"] = q["eps"]
                src.append("company filings (four quarters added)")
            elif y.get("sales") is not None:
                r.update(sales=y["sales"], np=y.get("np"), op=None)
                src.append("Yahoo Finance")
        for f in ("dep", "fin", "pbt", "tax", "eps"):               # lines the quarter table does not carry
            if r.get(f) is None and y.get(f) is not None:
                r[f] = y[f]
                if "Yahoo Finance" not in src:
                    src.append("Yahoo Finance")
        if r.get("reserves") in (0, 0.0):
            r["reserves"] = None
        if r.get("equity") is None and r.get("reserves") is not None and r.get("paid_up") is not None:
            r["equity"] = round(r["paid_up"] + r["reserves"], 2)      # shareholders' funds before revaluation reserves
            r["equity_est"] = True
        r["src"] = list(dict.fromkeys(src))
        if r.get("sales") is not None:
            rows.append(r)
    return rows[-YEARS:]


def build(sym):
    from . import company
    nse = nse_annual.fetch(sym)
    cons, sa = nse.get("Consolidated") or {}, nse.get("Standalone") or {}
    co = company.get(sym)
    yahoo = _yahoo(co)
    qsum = _quarter_sums(sym)
    try:
        sq = {q["end"]: q for q in company.standalone(sym)["annual"]}
    except Exception:
        sq = {}
    has_both = bool(cons) and bool(sa)
    if not cons and sa:                                            # no subsidiaries: NSE files only the standalone result, and it is the whole company
        cons = sa
    elif not sa and cons:
        sa = {}
    out = {"sym": sym, "fetched": datetime.now().isoformat(timespec="seconds"), "has_both": has_both,
           "Consolidated": _merge(cons, yahoo, qsum), "Standalone": _merge(sa, {}, {}, sq) if has_both else []}
    return out


def get(sym, refresh=False):
    con = db.connect()
    con.execute(SCHEMA)
    row = con.execute("SELECT body, fetched FROM annual_cache WHERE sym=?", (sym,)).fetchone()
    if row and not refresh and time.time() - row["fetched"] < TTL:
        return json.loads(row["body"])
    d = build(sym)
    con.execute("INSERT OR REPLACE INTO annual_cache VALUES(?,?,?)", (sym, json.dumps(d), time.time()))
    con.commit()
    return d


if __name__ == "__main__":
    d = build(sys.argv[1] if len(sys.argv) > 1 else "TITAN")
    print("has_both", d["has_both"])
    for b in ("Consolidated", "Standalone"):
        print(b)
        for r in d[b]:
            print(" ", r["end"], {k: r.get(k) for k in ("sales", "op", "np", "eps", "equity", "borrowings", "cfo")}, r["src"])
