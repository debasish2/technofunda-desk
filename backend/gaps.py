"""What is missing from the data, stock by stock.

    python -m backend.gaps          # writes data/gap_report.json and data/gap_report.csv, prints a summary

For every NSE stock in the universe it records what the site holds (price history, checked quarterly results, ownership and
pledge, insider trades, bank NPA) and which gaps apply. Read-only: nothing is fetched and nothing is changed.
"""
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from . import db, market

DATA = Path(__file__).resolve().parent.parent / "data"
EXPECTED_QUARTER = "2026-06-30"          # the newest quarter whose results deadline (45 days) has passed
MCAP_BANDS = [(20000, "20,000 Cr+"), (5000, "5,000-20,000 Cr"), (1000, "1,000-5,000 Cr"), (500, "500-1,000 Cr"), (0, "under 500 Cr / unknown")]

CODES = {
    "not_loaded": "Never loaded: no quarterly results fetched for this stock at all",
    "no_scrip": "No BSE code, so its filings cannot be looked up",
    "no_quarters": "Loaded, but no quarter passed the checks",
    "few_quarters": "Fewer than 4 clean quarters (growth and trend figures unavailable)",
    "stale": "Newest clean quarter is older than Jun 2026",
    "fin_no_npa": "Bank / lender / insurer with no NPA data",
    "no_holding": "No promoter-holding history",
    "no_insider_fetch": "Insider and pledge data never fetched",
}


def band(m):
    for lo, name in MCAP_BANDS:
        if (m or 0) >= lo:
            return name
    return MCAP_BANDS[-1][1]


def build():
    con = db.connect()
    mcon = market.connect()
    uni = mcon.execute("SELECT u.sym, u.name, c.mcap, c.industry FROM universe u LEFT JOIN class c ON c.sym=u.sym").fetchall()
    bars = {r[0]: r[1] for r in mcon.execute("SELECT sym, COUNT(*) FROM mbar GROUP BY sym")}
    stock = {r["sym"]: dict(r) for r in con.execute("SELECT sym, kind, desk, scrip FROM stock")}
    q = {r["sym"]: dict(r) for r in con.execute(
        "SELECT sym, COUNT(*) n, MAX(qend) newest FROM quarter WHERE flags='' AND sales IS NOT NULL AND op IS NOT NULL AND np IS NOT NULL GROUP BY sym")}
    any_q = {r[0] for r in con.execute("SELECT DISTINCT sym FROM quarter")}
    src = {}
    for r in con.execute("SELECT sym, qend, source FROM quarter WHERE flags=''"):
        if r["sym"] not in src or r["qend"] > src[r["sym"]][0]:
            src[r["sym"]] = (r["qend"], r["source"])
    hold = {r[0] for r in con.execute("SELECT DISTINCT sym FROM holding")}
    disc = {r[0] for r in con.execute("SELECT sym FROM disc_fetch")}
    npa = {r[0] for r in con.execute("SELECT DISTINCT sym FROM npa")}
    rows = []
    for sym, name, mcap, industry in uni:
        s, qq = stock.get(sym), q.get(sym)
        gaps = []
        fin = bool(s and s["kind"] == "fin")
        if s is None or (sym not in any_q and not fin):
            gaps.append("not_loaded")
        else:
            if not s["scrip"]:
                gaps.append("no_scrip")
            if qq is None:
                gaps.append("no_quarters")
            else:
                if qq["n"] < 4:
                    gaps.append("few_quarters")
                if qq["newest"] < EXPECTED_QUARTER:
                    gaps.append("stale")
        if fin and sym not in npa:
            gaps.append("fin_no_npa")
        if sym not in hold:
            gaps.append("no_holding")
        if sym not in disc:
            gaps.append("no_insider_fetch")
        rows.append({"sym": sym, "name": name, "industry": industry, "mcap_cr": None if mcap is None else round(mcap), "band": band(mcap),
                     "kind": "financial" if fin else "company", "desk": bool(s and s["desk"]), "bse_code": s["scrip"] if s else None,
                     "price_bars": bars.get(sym, 0), "clean_quarters": qq["n"] if qq else 0, "newest_quarter": qq["newest"] if qq else None,
                     "newest_source": src.get(sym, (None, None))[1], "gaps": gaps})
    return rows


def main():
    rows = build()
    (DATA / "gap_report.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    with open(DATA / "gap_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "industry", "mcap_cr", "band", "kind", "price_bars", "clean_quarters", "newest_quarter", "newest_source", "gaps"])
        for r in sorted(rows, key=lambda r: -(r["mcap_cr"] or 0)):
            w.writerow([r["sym"], r["name"], r["industry"], r["mcap_cr"], r["band"], r["kind"], r["price_bars"], r["clean_quarters"],
                        r["newest_quarter"], r["newest_source"], ";".join(r["gaps"])])
    n = len(rows)
    core = [r for r in rows if not ({"not_loaded", "no_scrip", "no_quarters", "few_quarters", "stale", "fin_no_npa"} & set(r["gaps"]))]
    print(f"{n} stocks in the universe; {len(core)} ({len(core) / n:.0%}) have no results gap")
    cnt = Counter(g for r in rows for g in r["gaps"])
    print("\nGaps (a stock can have several):")
    for code, text in CODES.items():
        print(f"  {cnt.get(code, 0):>5}  {text}")
    print("\nBy market-cap band: stocks / with a results gap")
    for _, name in MCAP_BANDS:
        grp = [r for r in rows if r["band"] == name]
        bad = [r for r in grp if {"not_loaded", "no_scrip", "no_quarters", "few_quarters", "stale", "fin_no_npa"} & set(r["gaps"])]
        print(f"  {name:<26} {len(grp):>5} / {len(bad):>4}")
    fin = [r for r in rows if r["kind"] == "financial"]
    print(f"\nFinancials: {len(fin)} stocks, {sum('fin_no_npa' in r['gaps'] for r in fin)} without NPA data")
    big = [r for r in sorted(rows, key=lambda r: -(r["mcap_cr"] or 0)) if {"not_loaded", "no_scrip", "no_quarters", "few_quarters", "stale", "fin_no_npa"} & set(r["gaps"])][:25]
    print("\nLargest stocks with a results gap:")
    for r in big:
        print(f"  {r['sym']:<12} {r['mcap_cr'] or 0:>9,} Cr  {r['kind']:<9} {', '.join(g for g in r['gaps'] if g not in ('no_holding', 'no_insider_fetch'))}")
    print("\nFiles: data/gap_report.csv (open in Excel) and data/gap_report.json")


if __name__ == "__main__":
    main()
