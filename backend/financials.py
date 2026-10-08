"""Banks, lenders and insurers: the stocks the ordinary results logic cannot read.

    python -m backend.financials --top 800     # find and load financial companies, most liquid first

An ordinary company reports sales, operating profit and net profit. A bank reports interest earned and
expended, provisions, and no "operating profit" in the usual sense, so margin, ROCE and sales-based filters
mean nothing for it. For financials the stored quarters are mapped as:

    sales  <- net interest income (banks, lenders)  or total revenue (insurers, exchanges, brokers)
    op     <- pre-tax profit                        (stands in for operating profit in momentum and cycle stage)
    np     <- net profit to shareholders

so the result grade, profit growth, momentum, PE and ROE work unchanged, and P/B is added. Margin and ROCE
are blank for them. One basis per stock: if most quarters have net interest income, every quarter must.

Source is Yahoo only (NSE's filing feed uses a different bank taxonomy and ends at Dec 2024). Yahoo's bank data
is patchy: some stocks have only old quarters, which makes them stale, and stale stocks never pass a filter.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import yfinance as yf

from . import db, indicators

CR = 1e7
WORKERS = 4


def retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def fetch_fin(sym):
    """Worker-thread safe: returns (profile, quarters, why). profile is None when the stock is not a usable financial."""
    tk = yf.Ticker(sym + ".NS")
    info = retry(lambda: tk.info) or {}
    if info.get("sector") != "Financial Services":
        return None, None, "not a financial company"
    if info.get("financialCurrency") not in (None, "INR"):
        return None, None, f"statements in {info['financialCurrency']}"
    q = retry(lambda: tk.quarterly_income_stmt)
    if q is None or q.empty:
        return None, None, "no Yahoo quarters"

    def get(col, *names):
        for n in names:
            if n in q.index and not pd.isna(q.loc[n, col]):
                return float(q.loc[n, col]) / CR
        return None

    raw = []
    for col in sorted(q.columns):
        raw.append({"qend": pd.Timestamp(col).strftime("%Y-%m-%d"), "nii": get(col, "Net Interest Income"),
                    "rev": get(col, "Total Revenue", "Operating Revenue"),
                    "pbt": get(col, "Pretax Income"), "np": get(col, "Net Income Common Stockholders", "Net Income")})
    use_nii = sum(r["nii"] is not None for r in raw) >= 3
    base = "nii" if use_nii else "rev"
    rows = [{"qend": r["qend"], "sales": round(r[base], 1), "op": round(r["pbt"], 1), "np": round(r["np"], 1)}
            for r in raw if r[base] is not None and r["pbt"] is not None and r["np"] is not None and r[base] > 0]
    if len(rows) < 3:
        return None, None, "too few usable quarters"

    equity = None
    try:                                         # book value per share x shares is fresher than the annual balance sheet
        if info.get("bookValue") and info.get("sharesOutstanding"):
            equity = info["bookValue"] * info["sharesOutstanding"] / CR
        else:
            bs = tk.balance_sheet
            for n in ("Stockholders Equity", "Common Stock Equity"):
                if n in bs.index and not pd.isna(bs.loc[n, bs.columns[0]]):
                    equity = float(bs.loc[n, bs.columns[0]]) / CR
                    break
    except Exception:
        pass
    prof = {"name": info.get("longName") or sym, "industry": info.get("industry"),
            "shares_cr": (info.get("sharesOutstanding") or 0) / CR, "equity": equity, "basis": base}
    return prof, rows, ""


def store(sym, prof, rows, con):
    con.execute("INSERT OR REPLACE INTO stock(sym,name,sector,shares_cr,cap_employed,equity,updated,desk,kind) "
                "VALUES(?,?,?,?,?,?,?,0,'fin')",
                (sym, prof["name"], prof["industry"], prof["shares_cr"], None, prof["equity"],
                 datetime.now().isoformat(timespec="seconds")))
    con.execute("DELETE FROM quarter WHERE sym=? AND source='Yahoo (fin)'", (sym,))
    con.executemany("INSERT INTO quarter(sym,qend,sales,op,np,eps,basis,source,filed,ref,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [(sym, r["qend"], r["sales"], r["op"], r["np"], None, "Consolidated", "Yahoo (fin)", None,
                      f"https://finance.yahoo.com/quote/{sym}.NS/financials/", "") for r in rows])


def refresh_all(log=print):
    """Weekly upkeep: re-fetch every stored financial company."""
    con = db.connect()
    syms = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE kind='fin'")]
    ok = bad = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_fin, s): s for s in syms}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                prof, rows, why = f.result()
            except Exception:
                bad += 1
                continue
            if prof is None:
                bad += 1
                continue
            store(sym, prof, rows, con)
            ok += 1
    con.commit()
    return ok, bad


def discover(top, log=print):
    """Walk the liquidity-ranked stocks not yet loaded; keep the ones that turn out to be financial companies."""
    con = db.connect()
    snap = indicators.snapshot()
    have = {r["sym"] for r in con.execute("SELECT sym FROM stock")}
    syms = [s for s in snap[snap["value_cr"].notna()].sort_values("value_cr", ascending=False).index if s not in have][:top]
    log(f"checking {len(syms)} not-yet-loaded stocks for financial companies, {WORKERS} workers")
    added, skipped, t0 = 0, {}, time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_fin, s): s for s in syms}
        for i, f in enumerate(as_completed(futs), 1):
            sym = futs[f]
            try:
                prof, rows, why = f.result()
            except Exception as e:
                skipped[sym] = type(e).__name__
                continue
            if prof is None:
                skipped[sym] = why
                continue
            store(sym, prof, rows, con)
            added += 1
            if added % 20 == 0:
                con.commit()
                log(f"  {added} financial companies added after checking {i} | {(time.time() - t0) / 60:.1f} min")
    con.commit()
    from collections import Counter
    log(f"done: {added} financial companies added; the rest: {dict(Counter(skipped.values()).most_common(6))}")
    return added


def reclassify(log=print):
    """Stocks loaded earlier as ordinary companies that are really financial: banks with no usable quarters, and
    lenders stuck on old NSE-format rows. Re-load them with the financial mapping. Their old rows used a different
    meaning of 'sales', so they are replaced, not mixed; they also leave the Desk (which cannot show a bank)."""
    con = db.connect()
    cands = [r["sym"] for r in con.execute("""
        SELECT sym FROM stock WHERE COALESCE(kind,'corp')='corp' AND sym NOT IN
          (SELECT sym FROM quarter WHERE flags='' AND qend >= '2026-01-01')""")]
    log(f"reclassify: {len(cands)} ordinary-company rows have no recent quarters; checking which are financial")
    moved, kept = [], 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_fin, s): s for s in cands}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                prof, rows, why = f.result()
            except Exception:
                kept += 1
                continue
            if prof is None:
                kept += 1
                continue
            con.execute("DELETE FROM quarter WHERE sym=?", (sym,))
            store(sym, prof, rows, con)
            moved.append(sym)
    con.commit()
    log(f"reclassified {len(moved)} as financial companies; {kept} left as they were (not financial, or nothing usable)")
    log("moved: " + ", ".join(sorted(moved)[:40]))
    return moved


if __name__ == "__main__":
    if "--reclassify" in sys.argv:
        reclassify()
        sys.exit(0)
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 800
    discover(top)
