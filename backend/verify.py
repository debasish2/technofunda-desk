"""Check the Yahoo quarterly figures on the site against the companies' own results filings, stock by stock.

    python -m backend.verify                 # pass 1: text-layer PDFs for every stock, biggest companies first (resumable)
    python -m backend.verify --ocr           # pass 2: the stocks pass 1 could not read, using OCR (slow; resumable)
    python -m backend.verify --limit 50      # only the first 50 stocks still to do
    python -m backend.verify --redo          # ignore earlier results
    python -m backend.verify --syms A,B --ocr   # just these stocks (any status), with or without OCR
    python -m backend.verify --report        # summary of everything checked so far (also data/verify_report.json)

For each non-financial stock with a BSE code it downloads the newest results filings from BSE (up to two), reads the results table
with the arithmetic-checked reader in backend/sources/filing.py (built on the parser the site uses; a table that fails its own checks is never used as the
truth) and compares each clean quarter with the Yahoo row stored for the same quarter, using the tolerances of backend/accuracy.py:
sales 1.5%, operating margin 3 points, net profit 10%. Results go to two tables in data/setupdesk.db:
    verify_stock  one row per stock: ok | no_filing | unreadable | no_yahoo | error
    verify_q      one row per compared quarter, with both sets of figures and the gaps
Nothing else in the database is changed.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import requests

from . import accuracy, db, market
from .sources import bse, filing

SINCE = date(2026, 4, 1)
REPORT = Path(__file__).resolve().parent.parent / "data" / "verify_report.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_stock (
  sym TEXT PRIMARY KEY, status TEXT, n_compared INTEGER, n_agree INTEGER, tried INTEGER, ocr INTEGER, note TEXT, checked TEXT
);
CREATE TABLE IF NOT EXISTS verify_q (
  sym TEXT, qend TEXT, basis TEXT, ocr INTEGER,
  f_sales REAL, f_op REAL, f_np REAL, y_sales REAL, y_op REAL, y_np REAL,
  sales_gap REAL, op_gap REAL, np_gap REAL, agrees INTEGER, comparable INTEGER, checked TEXT,
  PRIMARY KEY (sym, qend)
);
"""
EXTRA_COLS = (("f_eps", "REAL"), ("ref", "TEXT"), ("filed", "TEXT"), ("strong", "INTEGER"), ("owners_ok", "INTEGER"))      # what the filing said besides the three compared figures, and where it came from


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(verify_q)")}
    for name, typ in EXTRA_COLS:
        if name not in have:
            con.execute(f"ALTER TABLE verify_q ADD COLUMN {name} {typ}")
    return con


def todo(con, ocr, redo, limit, only=None):
    mcon = market.connect()
    cap = {r[0]: r[1] or 0 for r in mcon.execute("SELECT sym, mcap FROM class")}
    done = {r["sym"]: r["status"] for r in con.execute("SELECT sym, status FROM verify_stock")}
    ocr_done = {r["sym"] for r in con.execute("SELECT sym FROM verify_stock WHERE ocr=1")}
    syms = [r["sym"] for r in con.execute(
        "SELECT sym FROM stock s WHERE COALESCE(kind,'')!='fin' AND scrip IS NOT NULL AND EXISTS("
        "SELECT 1 FROM quarter q WHERE q.sym=s.sym AND q.source='Yahoo' AND q.flags='')")]
    if only:
        syms = [s for s in syms if s in only]
    elif ocr:
        syms = [s for s in syms if done.get(s) == "unreadable" and (redo or s not in ocr_done)]      # skip the ones OCR has already had a go at
    elif not redo:
        syms = [s for s in syms if s not in done]
    syms.sort(key=lambda s: -cap.get(s, 0))
    return syms[:limit] if limit else syms


def _swapped(q, y, yahoo):
    """The filing's figures for this quarter-end are not Yahoo's for it but are Yahoo's for another quarter: the columns were read in the wrong order
    (some filings list current, year-ago, preceding). Such a row says nothing about either source."""
    near = lambda a, b: a is not None and b is not None and abs(a - b) <= 0.01 * abs(b) + 0.15
    if near(q["sales"], y["sales"]) and near(q["np"], y["np"]):
        return False
    return any(qe != q["qend"] and near(q["sales"], o["sales"]) and near(q["np"], o["np"]) for qe, o in yahoo.items())


def check(sym, scrip, yahoo, ocr):
    """-> (status, compared rows, note, filings tried)"""
    sess = requests.Session()
    try:
        ann = bse.result_announcements(scrip, SINCE, session=sess)
    except Exception as e:
        return "error", [], f"announcements: {type(e).__name__}", 0
    if not ann:
        return "no_filing", [], "", 0
    rows, tried, parsed, note = [], 0, False, ""
    for a in ann:
        if tried >= 2 or len(rows) >= 3:
            break
        try:
            pdf = bse.fetch_pdf(a["ATTACHMENTNAME"], sess)
        except Exception as e:
            note = f"download: {type(e).__name__}"
            continue
        if not pdf:
            continue
        tried += 1
        try:
            qs = filing.quarters_from_pdf(pdf, use_ocr=ocr, filed=a["DT_TM"], basis=None)
        except Exception as e:
            note = f"parse: {type(e).__name__}"
            qs = []
        clean = [q for q in qs if not q["flags"] and q["sales"] is not None and q["op"] is not None and q["np"] is not None]
        parsed = parsed or bool(clean)
        for q in clean:
            y = yahoo.get(q["qend"])
            if y and _swapped(q, y, yahoo):
                note = "column order: a figure matches another quarter"
                continue
            if y and not any(r["qend"] == q["qend"] for r in rows):
                rows.append({"qend": q["qend"], "basis": q["basis"], "ocr": bool(q.get("ocr", False)), "eps": q.get("eps"), "strong": int(bool(q.get("strong"))), "owners_ok": int(bool(q.get("owners_ok"))), "filed": a["DT_TM"][:10],
                             "ref": bse.PDF.format(where="AttachHis", name=a["ATTACHMENTNAME"]),
                             "f": {k: q[k] for k in ("sales", "op", "np")}, "y": {k: y[k] for k in ("sales", "op", "np")},
                             **accuracy.compare(q, y)})
                # Yahoo's rows are consolidated where a company has subsidiaries. A standalone filing table whose sales differ is a
                # different basis, not a Yahoo error, so it is kept but not counted for or against Yahoo.
                rows[-1]["comparable"] = q["basis"] == "Consolidated" or abs(rows[-1]["sales_gap_pct"] or 0) <= 1.5
    if rows:
        return "ok", rows, note, tried
    return ("no_yahoo" if parsed else ("unreadable" if tried else "no_filing")), [], note, tried


def save(con, sym, status, rows, note, tried, ocr):
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("DELETE FROM verify_q WHERE sym=?", (sym,))
    for r in rows:
        con.execute("INSERT OR REPLACE INTO verify_q(sym,qend,basis,ocr,f_sales,f_op,f_np,y_sales,y_op,y_np,sales_gap,op_gap,np_gap,agrees,comparable,checked,f_eps,ref,filed,strong,owners_ok)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sym, r["qend"], r["basis"], int(r["ocr"]), r["f"]["sales"], r["f"]["op"], r["f"]["np"], r["y"]["sales"], r["y"]["op"], r["y"]["np"],
                     r["sales_gap_pct"], r["op_margin_gap_pts"], r["np_gap_pct"], int(r["agrees"]), int(r["comparable"]), now, r.get("eps"), r.get("ref"), r.get("filed"), r.get("strong", 0), r.get("owners_ok", 0)))
    con.execute("INSERT OR REPLACE INTO verify_stock VALUES(?,?,?,?,?,?,?,?)",
                (sym, status, len(rows), sum(r["agrees"] and r["comparable"] for r in rows), tried, int(ocr), note, now))
    con.commit()


def run(ocr=False, redo=False, limit=None, workers=4, only=None):
    con = connect()
    syms = todo(con, ocr, redo, limit, only)
    scrip = {r["sym"]: r["scrip"] for r in con.execute("SELECT sym, scrip FROM stock")}
    log(f"{len(syms)} stocks to check ({'OCR pass' if ocr else 'text pass'}), {workers} at a time")
    jobs = {}
    for s in syms:
        yahoo = {r["qend"]: dict(r) for r in con.execute(
            "SELECT qend, sales, op, np FROM quarter WHERE sym=? AND source='Yahoo' AND flags=''", (s,))}
        jobs[s] = yahoo
    t0, counts = time.time(), {}
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(check, s, scrip[s], jobs[s], ocr): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                status, rows, note, tried = fut.result()
            except Exception as e:
                status, rows, note, tried = "error", [], f"{type(e).__name__}: {e}", 0
            save(con, s, status, rows, note, tried, ocr)
            counts[status] = counts.get(status, 0) + 1
            if i % 25 == 0 or i == len(syms):
                el = time.time() - t0
                log(f"{i}/{len(syms)} done ({el / 60:.0f} min, about {el / i * (len(syms) - i) / 60:.0f} min to go)  {counts}")
    log(f"finished: {counts}")


def classify(r):
    """agree | differ | suspect. Yahoo's revenue often leaves out 'other operating revenue', so sales within 3% counts as the same figure;
    a gap that is far too large to be a real difference (a wrong column, a unit slip) is the reader's mistake, not Yahoo's."""
    s, o, n = (abs(r[k]) if r[k] is not None else 0 for k in ("sales_gap", "op_gap", "np_gap"))
    if s > 25 or n > 50 or o > 20:
        return "suspect"
    return "agree" if s <= 3 and o <= 3 and n <= 10 else "differ"


def report():
    con = connect()
    st = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM verify_stock GROUP BY status")}
    allq = [dict(r) for r in con.execute("SELECT * FROM verify_q")]
    qs = [r for r in allq if r["comparable"]]
    out = {"stocks": st, "quarters_read": len(allq), "quarters_comparable": len(qs), "quarters_other_basis": len(allq) - len(qs)}
    if qs:
        for r in qs:
            r["cls"] = classify(r)
        cnt = {k: sum(r["cls"] == k for r in qs) for k in ("agree", "differ", "suspect")}
        usable = cnt["agree"] + cnt["differ"]
        out["classes"] = cnt
        out["agree_pct_of_trusted_reads"] = round(cnt["agree"] / max(usable, 1) * 100, 1)
        ok = lambda k, lim: round(sum(abs(r[k] or 0) <= lim for r in qs if r["cls"] != "suspect") / max(usable, 1) * 100, 1)
        out["within_tolerance_pct"] = {"sales_3pct": ok("sales_gap", 3), "sales_exact_1.5pct": ok("sales_gap", 1.5), "margin_3pts": ok("op_gap", 3), "profit_10pct": ok("np_gap", 10),
                                       "profit_1pct": ok("np_gap", 1)}
        out["stocks_to_review"] = len({r["sym"] for r in qs if r["cls"] == "differ"})
        import csv
        mcap = {r[0]: r[1] for r in market.connect().execute("SELECT sym, mcap FROM class")}
        with open(REPORT.with_name("verify_review.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "mcap_cr", "quarter", "verdict", "basis", "filing_sales", "yahoo_sales", "sales_gap_%", "margin_gap_pts", "filing_profit", "yahoo_profit", "profit_gap_%"])
            for r in sorted((r for r in qs if r["cls"] != "agree"), key=lambda r: (r["cls"], -(mcap.get(r["sym"]) or 0))):
                w.writerow([r["sym"], round(mcap.get(r["sym"]) or 0), r["qend"], r["cls"], r["basis"], r["f_sales"], r["y_sales"], r["sales_gap"], r["op_gap"], r["f_np"], r["y_np"], r["np_gap"]])
    REPORT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--report" in a:
        report()
    else:
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else None
        only = set(a[a.index("--syms") + 1].split(",")) if "--syms" in a else None
        run(ocr="--ocr" in a, redo="--redo" in a, limit=lim, only=only)
        report()
