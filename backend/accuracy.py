"""How far can the Yahoo quarterly figures on the site be trusted?

    python -m backend.accuracy [N]      # N stocks (default 30), writes data/accuracy_report.json

For a random sample of non-financial stocks it downloads the newest results filings from BSE, reads the results table with
the same arithmetic-checked parser the site uses (bse.quarters_from_pdf), and compares every clean quarter in the filing
(current, previous, year-ago) with the Yahoo row the site stores for the same quarter. A filing row that failed its own
checks is not used as the truth. Differences are reported per metric:
    sales        % difference
    operating    percentage points of sales (operating margin)
    net profit   % difference
and a quarter 'agrees' within the tolerances the stitch step uses for the same purpose (sales 1.5%, margin 3 points, profit 10%).
"""
import json
import random
import sys
import time
from datetime import date
from pathlib import Path

import requests

from . import db
from .sources import bse

REPORT = Path(__file__).resolve().parent.parent / "data" / "accuracy_report.json"
SINCE = date(2026, 4, 1)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def sample(con, n, seed=7):
    pool = [r["sym"] for r in con.execute(
        "SELECT sym FROM stock s WHERE COALESCE(kind,'')!='fin' AND scrip IS NOT NULL AND EXISTS("
        "SELECT 1 FROM quarter q WHERE q.sym=s.sym AND q.source='Yahoo' AND q.qend>='2026-03-31' AND q.flags='')")]
    random.Random(seed).shuffle(pool)
    return pool[:n]


def compare(filing, y):
    sales_gap = (y["sales"] / filing["sales"] - 1) * 100 if filing["sales"] else None
    op_gap = (y["op"] - filing["op"]) / filing["sales"] * 100 if filing["sales"] and filing["op"] is not None else None
    np_gap = (y["np"] - filing["np"]) / max(abs(filing["np"]), 1.0) * 100 if filing["np"] is not None else None
    ok = (sales_gap is not None and abs(sales_gap) <= 1.5 and (op_gap is None or abs(op_gap) <= 3)
          and (np_gap is None or abs(np_gap) <= 10))
    return {"sales_gap_pct": None if sales_gap is None else round(sales_gap, 2),
            "op_margin_gap_pts": None if op_gap is None else round(op_gap, 2),
            "np_gap_pct": None if np_gap is None else round(np_gap, 2), "agrees": ok}


def check(sym, con, sess):
    row = con.execute("SELECT scrip FROM stock WHERE sym=?", (sym,)).fetchone()
    ann = bse.result_announcements(row["scrip"], SINCE, session=sess)
    yahoo = {r["qend"]: dict(r) for r in con.execute(
        "SELECT qend,sales,op,np FROM quarter WHERE sym=? AND source='Yahoo' AND flags=''", (sym,))}
    out, tried = [], 0
    for a in ann:
        if tried >= 2 or len(out) >= 3:
            break
        pdf = bse.fetch_pdf(a["ATTACHMENTNAME"], sess)
        if not pdf:
            continue
        tried += 1
        for use_ocr in (False, True):
            try:
                qs = bse.quarters_from_pdf(pdf, use_ocr=use_ocr, filed=a["DT_TM"], basis=None)
            except Exception as e:
                log(f"  {sym}: parse error {type(e).__name__}")
                qs = []
            clean = [q for q in qs if not q["flags"] and q["sales"] is not None and q["op"] is not None and q["np"] is not None]
            if clean or not use_ocr:
                if clean:
                    break
        for q in clean:
            y = yahoo.get(q["qend"])
            if y and not any(o["qend"] == q["qend"] for o in out):
                out.append({"qend": q["qend"], "basis": q["basis"], "ocr": q.get("ocr", False),
                            "filing": {k: q[k] for k in ("sales", "op", "np")},
                            "yahoo": {k: y[k] for k in ("sales", "op", "np")}, **compare(q, y)})
    return out


def main(n=30):
    con = db.connect()
    sess = requests.Session()
    syms = sample(con, n)
    report = {}
    for sym in syms:
        t = time.time()
        try:
            rows = check(sym, con, sess)
        except Exception as e:
            rows = []
            log(f"{sym}: failed {type(e).__name__}: {e}")
        report[sym] = rows
        ag = sum(r["agrees"] for r in rows)
        log(f"{sym:<12} {time.time() - t:>4.0f}s  {len(rows)} quarters compared, {ag} agree")
        REPORT.write_text(json.dumps(report, indent=1), encoding="utf-8")
    allq = [r for rows in report.values() for r in rows]
    stocks = [s for s, rows in report.items() if rows]
    if allq:
        ag = sum(r["agrees"] for r in allq)
        med = lambda k: sorted(abs(r[k]) for r in allq if r[k] is not None)[len([r for r in allq if r[k] is not None]) // 2]
        log(f"SUMMARY: {len(stocks)} of {len(syms)} stocks readable; {len(allq)} quarters compared; {ag} agree ({ag / len(allq):.0%}); "
            f"median gap: sales {med('sales_gap_pct'):.2f}%, margin {med('op_margin_gap_pts'):.2f} pts, profit {med('np_gap_pct'):.2f}%")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
