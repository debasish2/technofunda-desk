"""Load asset quality (gross / net NPA) and, where Yahoo has nothing, the quarterly P&L, for the major banks.

    python -m backend.bank_job                    # the built-in list of large banks
    python -m backend.bank_job HDFCBANK SBIN      # just these

For each bank: find its results filings on NSE (newest first), parse each with sources/banks.py (OCR where the PDF is
a scan), and stop once the last five quarters are covered. Stored:
  * npa table      gross / net NPA and their ratios, from the standalone table (banks report NPAs standalone only)
  * quarter table  only for banks Yahoo has no recent quarters for (e.g. HDFC Bank): consolidated figures where found,
                   mapped like the other financials (sales = net interest income, op = pre-tax profit, np = net profit)
For banks that Yahoo does cover, the PDF's consolidated profit is compared with Yahoo's and the gaps are reported:
that is the accuracy check on Yahoo's bank figures.
"""
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests

from . import db
from .sources import banks, bse, nse

REPORT = Path(__file__).resolve().parent.parent / "data" / "bank_job_report.json"
TARGETS = ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK", "BANKBARODA", "PNB", "CANBK",
           "UNIONBANK", "IDFCFIRSTB", "FEDERALBNK", "AUBANK", "BANDHANBNK", "YESBANK", "INDIANB", "BANKINDIA",
           "MAHABANK", "RBLBANK", "CUB", "KTKBANK", "KARURVYSYA", "SOUTHBANK", "TMB", "DCBBANK", "IDBI",
           "CENTRALBK", "IOB", "PSB", "JSFB", "UJJIVANSFB", "EQUITASBNK", "CSBBANK"]
SINCE = date(2025, 4, 1)
MAX_FILINGS = 4         # filings with a parsed results table
MAX_DOWNLOADS = 10      # all PDFs fetched, including cover letters and presentations


def quarter_ends(today=None):
    """The last five quarter ends whose results should be out by now (banks report within ~50 days)."""
    today = today or date.today()
    cands = []
    for yy in (today.year - 2, today.year - 1, today.year):
        for mm, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = date(yy, mm, dd)
            if (today - d).days >= 50:
                cands.append(d.isoformat())
    return cands[-5:]


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def process(sym, con, report):
    want = set(quarter_ends()[-3:])
    cands = nse.result_announcements(sym, SINCE)
    got = {"Standalone": {}, "Consolidated": {}}      # basis -> {qend: (rank, column)}
    npa_got = {}                                      # qend -> (rank, asset-quality figures, url): kept even when the P&L column failed
    done, tried, downloads = [], 0, 0
    for c in cands:
        if tried >= MAX_FILINGS or downloads >= MAX_DOWNLOADS:
            break
        d = date.fromisoformat(c["date"])
        if any(abs((d - x).days) <= 3 for x in done):
            continue
        pdf = bse._fetch_url(c["url"], requests.Session())
        if not pdf:
            continue
        downloads += 1
        try:
            res = banks.parse_pdf(pdf, max_pages=14)
        except Exception as e:
            log(f"  {sym}: parse error {type(e).__name__} on {c['date']}")
            continue
        n = 0
        for rank, col in enumerate(res.pop("NPA", [])):         # tables that carry asset quality only
            if col["qend"] not in npa_got or rank + 10 < npa_got[col["qend"]][0]:
                npa_got[col["qend"]] = (rank + 10, col, c["url"])
        for rank, col in enumerate(res["Standalone"]):          # banks report NPAs standalone; a failed P&L check does not void them
            if col.get("qend") and (col.get("gnpa_pct") is not None or col.get("nnpa_pct") is not None):
                if col["qend"] not in npa_got or rank < npa_got[col["qend"]][0]:
                    npa_got[col["qend"]] = (rank, col, c["url"])
        for basis, cols in res.items():
            for rank, col in enumerate(cols):
                if col.get("flags") or "np" not in col or not col.get("qend"):
                    continue
                cur = got[basis].get(col["qend"])
                if cur is None or rank < cur[0]:
                    got[basis][col["qend"]] = (rank, col, c["url"])
                n += 1
        if n:
            done.append(d)
            tried += 1                                  # only filings that actually yielded a results table count
        have = set(got["Standalone"]) | set(got["Consolidated"])
        if want <= have and want <= set(npa_got):
            break
    # ---- store
    st = con.execute("SELECT kind FROM stock WHERE sym=?", (sym,)).fetchone()
    if st is None:
        return {"error": "not in the stock table"}
    npa_rows = 0
    for q, (rank, col, url) in sorted(npa_got.items()):
        con.execute("INSERT OR REPLACE INTO npa VALUES(?,?,?,?,?,?,?,?,?)",
                    (sym, q, col["gnpa"], col["nnpa"], col["gnpa_pct"], col["nnpa_pct"], "Standalone", "Bank PDF", url))
        npa_rows += 1
    basis = "Consolidated" if got["Consolidated"] else "Standalone"
    pl = got[basis]
    if len(pl) >= 3:
        med = sorted(c["nii"] for _, c, _ in pl.values())[len(pl) // 2]
        for q in [q for q, (_, c, _) in pl.items() if not 0.45 <= c["nii"] / med <= 2.2]:
            log(f"  {sym}: dropping {q}: net interest income {pl[q][1]['nii']:,.0f} against a typical {med:,.0f}")
            del pl[q]
    recent_yahoo = con.execute("SELECT COUNT(*) FROM quarter WHERE sym=? AND flags='' AND qend>='2026-01-01' "
                               "AND source IN ('Yahoo (fin)', 'Yahoo')", (sym,)).fetchone()[0]
    out = {"filings_parsed": tried, "quarters_found": sorted(pl), "npa_quarters": npa_rows, "basis": basis, "pl_from": "none"}
    if recent_yahoo == 0 and pl:
        con.execute("DELETE FROM quarter WHERE sym=?", (sym,))
        con.executemany("INSERT INTO quarter(sym,qend,sales,op,np,eps,basis,source,filed,ref,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        [(sym, q, col["nii"], col["pbt"] if col["pbt"] is not None else col["np"], col["np"], None, basis,
                          "Bank PDF", None, url, "") for q, (_, col, url) in sorted(pl.items())])
        con.execute("UPDATE stock SET kind='fin', desk=0 WHERE sym=?", (sym,))
        out["pl_from"] = "PDF (Yahoo had nothing recent)"
    elif pl:                                           # Yahoo covers it: use the PDF only to check Yahoo's figures
        diffs = {}
        for q, (_, col, _) in pl.items():
            y = con.execute("SELECT sales, np FROM quarter WHERE sym=? AND qend=? AND source='Yahoo (fin)'", (sym, q)).fetchone()
            if y:
                diffs[q] = {"pdf_np": col["np"], "yahoo_np": y["np"], "np_gap_pct": round((y["np"] / col["np"] - 1) * 100, 1) if col["np"] else None,
                            "pdf_nii": col["nii"], "yahoo_sales": y["sales"],
                            "nii_gap_pct": round((y["sales"] / col["nii"] - 1) * 100, 1) if col["nii"] else None}
        out["pl_from"] = "Yahoo (checked against the PDF)"
        out["yahoo_vs_pdf"] = diffs
    con.commit()
    return out


def main(syms):
    con = db.connect()
    report = {}
    for sym in syms:
        t = time.time()
        try:
            report[sym] = process(sym, con, report)
        except Exception as e:
            report[sym] = {"error": f"{type(e).__name__}: {e}"}
        r = report[sym]
        log(f"{sym:<11} {time.time() - t:>4.0f}s  " + (r.get("error") or
            f"{r['filings_parsed']} filing(s), {len(r['quarters_found'])} quarters ({r['basis']}), {r['npa_quarters']} NPA quarters, P&L: {r['pl_from']}"))
        REPORT.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


if __name__ == "__main__":
    main(sys.argv[1:] or TARGETS)
