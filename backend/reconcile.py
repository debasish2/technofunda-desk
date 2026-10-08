"""Settle the quarters where a company's own filing and Yahoo disagree.

    python -m backend.reconcile            # show what would change (nothing is written)
    python -m backend.reconcile --apply    # write the filing's figures into `quarter`

The verification (backend/verify.py) read each stock's latest results filings and compared them with the Yahoo rows the site holds.
This turns those comparisons into decisions, one quarter at a time:

  agree     filing and Yahoo say the same            keep the row, nothing to do
  adopt     the filing is shown to be the right one  replace Yahoo's figures with the filing's, keep the PDF link
  disputed  the two disagree and nothing says why    leave the row as it is and list it in data/reconcile_disputed.csv
  suspect   the filing read looks like a mistake     (a unit slip, a wrong column) leave it alone; it belongs to the reader's bug list

A filing is adopted only when its read is anchored by something independent of the figure being replaced:
  - sales within 3% of Yahoo's        the same table column was read; any gap in profit or margin is then a difference in definition
                                      (Yahoo adds back exceptional items and reconstructs operating profit), and the filing's is the company's own
  - net profit within 1% of Yahoo's   the same column was read although sales differ; the filing's sales is what the company reports as
                                      revenue from operations, Yahoo's often leaves out other operating income
Adopted rows are written with source 'BSE PDF (checked)', which backend/stitch.py leaves alone.
"""
import csv
import sys
from pathlib import Path

from . import db, market, verify

CHECKED = "BSE PDF (checked)"
OUT = Path(__file__).resolve().parent.parent / "data"


def _unit_slip(r):
    """A figure that is an exact power of ten away from Yahoo's is a misread scale (lakh read as crore), not a different number."""
    import math
    for a, b in ((r["f_sales"], r["y_sales"]), (r["f_op"], r["y_op"]), (r["f_np"], r["y_np"])):
        if a and b and a / b > 0:
            l = math.log10(a / b)
            if abs(l - round(l)) < 0.03 and round(l) != 0:
                return True
    return False


def decide(r):
    """-> (decision, reason) for one comparable verify_q row."""
    cls = verify.classify(r)
    if cls == "agree":
        return "agree", ""
    if r.get("strong") and not r["ocr"] and r.get("ref") and not _unit_slip(r):
        ratio = (r["f_sales"] or 0) / r["y_sales"] if r["y_sales"] else 0
        if 0.4 <= ratio <= 2.2:                   # gross revenue (excise included) runs to about twice Yahoo's; a ratio near 4 is a full-year column
            return "adopt", "the filing's own table balances on three or more of its arithmetic checks"
        return "disputed", "the filing balances but its sales are far from Yahoo's"
    if cls == "suspect":
        return "suspect", "gap too large to be a real difference"
    s, o, n = (abs(r[k] or 0) for k in ("sales_gap", "op_gap", "np_gap"))
    if r["ocr"]:
        return "disputed", "read from a scan"
    if not r.get("ref"):
        return "disputed", "source link not stored yet"
    if r["f_op"] and r["f_op"] > 0 and r["f_np"] > 0.85 * r["f_op"] and r["f_np"] > 1.1 * (r["y_np"] or 0):
        return "disputed", "profit above operating profit: the filing's profit line may be before exceptional items"
    if s <= 3:
        return "adopt", "same column; the gap is in how profit or operating profit is defined"
    if n <= 1:
        return "adopt", "same profit; the filing reports the larger revenue line"
    return "disputed", "sales and profit both differ"


def run(apply=False):
    con = verify.connect()
    q = db.connect()
    mcap = {r[0]: r[1] for r in market.connect().execute("SELECT sym, mcap FROM class")}
    adopt, disputed, counts = [], [], {}
    for r in (dict(x) for x in con.execute("SELECT * FROM verify_q WHERE comparable=1")):
        d, why = decide(r)
        counts[d] = counts.get(d, 0) + 1
        if d == "adopt":
            adopt.append((r, why))
        elif d == "disputed":
            disputed.append((r, why))
    print("decisions:", counts)
    cols = ["symbol", "mcap_cr", "quarter", "basis", "filing_sales", "yahoo_sales", "filing_op", "yahoo_op", "filing_profit", "yahoo_profit", "why", "pdf"]
    row = lambda r, why: [r["sym"], round(mcap.get(r["sym"]) or 0), r["qend"], r["basis"], r["f_sales"], r["y_sales"], r["f_op"], r["y_op"], r["f_np"], r["y_np"], why, r.get("ref")]
    for name, items in (("reconcile_changes.csv", adopt), ("reconcile_disputed.csv", disputed)):
        with open(OUT / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r, why in sorted(items, key=lambda t: -(mcap.get(t[0]["sym"]) or 0)):
                w.writerow(row(r, why))
    print(f"wrote data/reconcile_changes.csv ({len(adopt)}) and data/reconcile_disputed.csv ({len(disputed)})")
    if not apply:
        return
    n = 0
    for r, why in adopt:
        cur = q.execute("SELECT source FROM quarter WHERE sym=? AND qend=?", (r["sym"], r["qend"])).fetchone()
        if cur is None or cur["source"] == "NSE XBRL":
            continue                                          # official XBRL stays; a missing row is the nightly job's to fill
        np_ = r["f_np"]
        if r["basis"] == "Consolidated" and not r.get("owners_ok") and r["y_np"] is not None and abs(np_ - r["y_np"]) > 0.1 * abs(r["y_np"]) + 1:
            np_ = r["y_np"]       # the filing's profit line may include the minorities' share and its owners' line was not read; Yahoo's is the owners' profit
        q.execute("UPDATE quarter SET sales=?, op=?, np=?, eps=COALESCE(?, eps), basis=?, source=?, filed=?, ref=?, flags='' WHERE sym=? AND qend=?",
                  (r["f_sales"], r["f_op"], np_, r.get("f_eps"), r["basis"], CHECKED, r.get("filed"), r.get("ref"), r["sym"], r["qend"]))
        n += 1
    q.commit()
    print(f"{n} quarters now carry the company's own filing figures")


if __name__ == "__main__":
    run(apply="--apply" in sys.argv)
