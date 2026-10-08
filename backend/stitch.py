"""Fill quarters the filings could not supply with Yahoo's recent quarters.

    python -m backend.stitch            # every stock in the database

Order of trust: NSE XBRL > results PDFs that passed their checks > Yahoo. Yahoo only fills a
quarter that is missing, or whose PDF parse was flagged. Every Yahoo row says so in `source`,
so the dashboard can show it. Operating profit is Yahoo's Operating Income plus depreciation,
which matched the filings exactly or within a few percent on the companies checked.

Banks, NBFCs and insurers are skipped: Yahoo's "revenue" for them is net of interest, which is not
comparable with the gross figures in their filings.
"""
import sys

import pandas as pd
import yfinance as yf

from . import db

CR = 1e7
FINANCIAL = {"Financial Services"}


def yahoo_rows(sym, tk=None, info=None):
    tk = tk or yf.Ticker(sym + ".NS")
    info = info if info is not None else (tk.info or {})
    if info.get("sector") in FINANCIAL:
        return None, "financial company (not supported)"
    if info.get("financialCurrency") not in (None, "INR"):          # e.g. Infosys is reported in USD
        return None, f"Yahoo reports in {info['financialCurrency']}, not INR"
    q = tk.quarterly_income_stmt
    if q is None or q.empty:
        return [], "no Yahoo quarters"

    def get(col, *names):
        for n in names:
            if n in q.index and not pd.isna(q.loc[n, col]):
                return float(q.loc[n, col]) / CR
        return None

    out = []
    for col in sorted(q.columns):
        sales = get(col, "Total Revenue", "Operating Revenue")
        opi, dep = get(col, "Operating Income"), get(col, "Reconciled Depreciation", "Depreciation And Amortization In Income Statement")
        net = get(col, "Net Income Common Stockholders", "Net Income")
        if sales is None or net is None or opi is None or dep is None:
            continue
        out.append({"qend": pd.Timestamp(col).strftime("%Y-%m-%d"), "sales": round(sales, 1),
                    "op": round(opi + dep, 1), "np": round(net, 1)})
    return out, ""


def disagrees(filing, y):
    """True if a parsed filing row and Yahoo's row for the same quarter differ materially."""
    if not filing["sales"]:
        return True
    margin_gap = abs(filing["op"] - y["op"]) / filing["sales"] * 100          # percentage points of sales
    return (margin_gap > 3
            or abs(filing["sales"] - y["sales"]) / filing["sales"] > 0.015
            or abs(filing["np"] - y["np"]) / max(abs(filing["np"]), 1.0) > 0.10)


def stitch(sym, con):
    rows, why = yahoo_rows(sym)
    return apply_rows(sym, rows, why, con)


def apply_rows(sym, rows, why, con):
    """Write Yahoo's quarters for `sym`, filling only what the filings could not supply (see the module notes)."""
    con.execute("DELETE FROM quarter WHERE sym=? AND source='Yahoo'", (sym,))
    if rows is None:
        return 0, why
    good = {r["qend"]: r for r in con.execute("SELECT * FROM quarter WHERE sym=? AND flags=''", (sym,))}
    last = con.execute("SELECT sales FROM quarter WHERE sym=? AND flags='' ORDER BY qend DESC LIMIT 1", (sym,)).fetchone()
    added = 0
    for r in rows:
        e = good.get(r["qend"])
        source = "Yahoo"
        if e is not None:
            if e["source"] in ("NSE XBRL", "BSE PDF (checked)") or not disagrees(e, r):
                continue                              # official XBRL, or filing and Yahoo agree: keep the filing
            source = "Yahoo (PDF disagreed)"          # two sources, one wrong: use the consistently-defined one
        elif last and last["sales"] and not 1 / 25 <= r["sales"] / last["sales"] <= 25:
            continue                                  # a basis or unit mismatch, not a real move
        con.execute("DELETE FROM quarter WHERE sym=? AND qend=?", (sym, r["qend"]))   # replaces a flagged or disputed parse
        con.execute("INSERT INTO quarter(sym,qend,sales,op,np,eps,basis,source,filed,ref,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (sym, r["qend"], r["sales"], r["op"], r["np"], None, "Consolidated", source, None,
                     f"https://finance.yahoo.com/quote/{sym}.NS/financials/", ""))
        added += 1
    con.commit()
    return added, why


if __name__ == "__main__":
    con = db.connect()
    syms = [r["sym"] for r in con.execute("SELECT sym FROM stock ORDER BY sym")]
    for sym in syms:
        try:
            n, why = stitch(sym, con)
            print(f"ok    {sym:<12} +{n} Yahoo quarters {why}")
        except Exception as e:
            print(f"skip  {sym:<12} {type(e).__name__}: {e}")
