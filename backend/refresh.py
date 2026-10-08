"""Refresh the database: python -m backend.refresh symbols.txt"""
import sys
from datetime import datetime
from pathlib import Path

from . import db
from .sources import bse, nse, yahoo


def read_symbols(path):
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            p = [x.strip() for x in line.split(",", 1)]
            out.append((p[0].upper(), p[1] if len(p) > 1 else None))
    return out


def refresh(sym, sector, con, pdf=True):
    prof = yahoo.profile(sym)
    con.execute("INSERT OR REPLACE INTO stock(sym,name,sector,shares_cr,cap_employed,equity,debt,np_annual,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (sym, prof["name"], sector or prof["industry"], prof["shares_cr"], prof["cap_employed"], prof["equity"], prof["debt"], prof["np_annual"],
                 datetime.now().isoformat(timespec="seconds")))
    b = yahoo.bars(sym)
    con.executemany("INSERT OR REPLACE INTO bar VALUES(?,?,?,?,?,?,?)", [(sym, *x) for x in b])
    qs, scrip = nse.fetch_quarters(sym)
    for q in qs:
        q.setdefault("flags", [])
    con.execute("UPDATE stock SET scrip=? WHERE sym=?", (scrip, sym))
    if pdf and qs and scrip:
        from collections import Counter
        basis = Counter(q["basis"] for q in qs[-8:]).most_common(1)[0][0]
        newer = bse.fetch_missing(scrip, qs[-1]["qend"], {q["qend"]: q for q in qs}, sym=sym,
                                  basis=basis, last_sales=qs[-1]["sales"])
        qs += newer
    # PDF-parsed rows are rebuilt from source every run: a wrong row looks the same as a good one, so none are kept.
    # (In --no-pdf mode the PDF rows are left alone; only Yahoo rows are redone by the stitch step.)
    con.execute("DELETE FROM quarter WHERE sym=? AND source != 'NSE XBRL'" + ("" if pdf else " AND source LIKE 'Yahoo%'"), (sym,))
    con.executemany("INSERT OR REPLACE INTO quarter VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [(sym, q["qend"], q["sales"], q["op"], q["np"], q["eps"], q["basis"], q["source"], q["filed"],
                      q["ref"], "; ".join(q["flags"])) for q in qs])
    con.commit()
    return len(b), len(qs)


if __name__ == "__main__":
    # python -m backend.refresh symbols.txt [--no-pdf]
    #   --no-pdf: NSE history + prices + Yahoo only (about 30 s a stock, instead of ~15 min with PDF parsing)
    con = db.connect()
    pdf = "--no-pdf" not in sys.argv
    for sym, sector in read_symbols(sys.argv[1]):
        try:
            nb, nq = refresh(sym, sector, con, pdf=pdf)
            if not pdf:
                from . import stitch
                added, why = stitch.stitch(sym, con)
                nq += added
            print(f"ok    {sym:<12} {nb} bars, {nq} quarters")
        except Exception as e:
            print(f"skip  {sym:<12} {type(e).__name__}: {e}")
