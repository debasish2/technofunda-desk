"""Overnight job: build fundamentals for established stocks Yahoo has no quarterly figures for, from the exchanges' own filings.

    python scripts/overnight_widen.py

About 3 minutes a stock on three workers (several hours for ~100 stocks). Then it fills annual profit and debt for anything new, refreshes the saved
screens, and writes data/overnight_report.txt with what it found. Safe to stop at any time: every stock is saved as it finishes.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import auth, db, fundamentals as fu, indicators as ind, industries, market, screens, widen  # noqa: E402

REPORT = ROOT / "data" / "overnight_report.txt"
t0 = time.time()
con = db.connect()
before = con.execute("SELECT COUNT(*) FROM stock").fetchone()[0]
sk = json.loads(widen.SKIPPED.read_text(encoding="utf-8"))
have = {r["sym"] for r in con.execute("SELECT sym FROM stock")}
cap = dict(market.connect().execute("SELECT sym, mcap FROM class").fetchall())
todo = sorted((s for s, why in sk.items() if why in ("no quarters", "no Yahoo quarters") and s not in have and (cap.get(s) or 0) >= 500), key=lambda s: -(cap.get(s) or 0))
first = ["AEGISLOG", "VRLLOG", "ANTELOPUS", "DEEPINDS", "JGCHEM"]       # the ones the scans were missing: tried again with the OCR-capable reader
todo = first + [x for x in todo if x not in first]
widen.log(f"overnight: {len(todo)} stocks to try")
widen.stage_filings(todo)
widen.backfill_annual_profit()
widen.backfill_debt()
data = ind.load()
snap = industries.enrich(ind.snapshot(data), data)
ft = fu.snapshot()
screens.snapshot_all(db.connect(), snap, ft)
user = list(auth._users())[0] if auth.enabled() else "me"
lines = [f"finished {datetime.now():%Y-%m-%d %H:%M}, took {(time.time() - t0) / 3600:.1f} hours",
         f"stocks held: {before} -> {db.connect().execute('SELECT COUNT(*) FROM stock').fetchone()[0]}",
         f"stocks with fundamentals now: {int((~ft['stale']).sum())}; with a 3-year profit figure: {int(ft['profit_cagr3'].notna().sum())}"]
for r in screens.summary(db.connect(), user):
    lines.append(f"saved screen {r['name']}: {r['count']} stocks")
REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
