"""Widen the fundamentals coverage beyond the Desk's own stocks, for the screener.

    python -m backend.widen --top 1000          # stage 1: Yahoo's recent quarters, most liquid stocks first (fast)
    python -m backend.widen --history           # stage 2: NSE's official history for those stocks (slower, once)

Stocks added here are flagged desk=0: the screener sees them, the Desk (which loads every desk=1 stock's full
price history into one page) does not. They need no price bars of their own, since the market database already
holds every stock's prices.

Stage 1 gives each stock its last ~5 quarters, enough for the newest result's grade, margins, PE, ROCE and ROE.
Stage 2 adds the older NSE history, which the cycle-stage and earnings-momentum filters need. Both are
resumable: stocks already done are skipped. Banks, NBFCs and insurers are skipped (different results layout),
as are companies whose statements Yahoo reports in a currency other than rupees.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yfinance as yf

from . import db, indicators, market, stitch
from .sources import bse, nse, yahoo

SKIPPED = Path(__file__).resolve().parent.parent / "data" / "widen_skipped.json"
WORKERS = 4


def log(msg):
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def yahoo_stage(sym):
    """Runs in a worker thread: only fetches, never touches the database."""
    tk = yf.Ticker(sym + ".NS")
    info = retry(lambda: tk.info) or {}
    if info.get("sector") in stitch.FINANCIAL:
        return sym, None, None, "financial company"
    if info.get("financialCurrency") not in (None, "INR"):
        return sym, None, None, f"statements in {info['financialCurrency']}"
    prof = retry(lambda: yahoo.profile(sym, tk=tk, info=info))
    rows, why = retry(lambda: stitch.yahoo_rows(sym, tk=tk, info=info))
    if not rows:
        return sym, None, None, why or "no quarters"
    return sym, prof, rows, ""


def history_stage(sym):
    qs, scrip = retry(lambda: nse.fetch_quarters(sym))
    rows, why = retry(lambda: stitch.yahoo_rows(sym))
    return sym, qs, scrip, rows, why


def candidates(n):
    snap = indicators.snapshot()
    have = {r["sym"] for r in db.connect().execute("SELECT sym FROM stock")}
    ranked = snap[snap["value_cr"].notna()].sort_values("value_cr", ascending=False)
    return [s for s in ranked.index if s not in have][:n]


def candidates_mcap(min_mcap):
    """Every stock worth at least `min_mcap` crore (BSE's figure) that the screener does not hold yet, banks and lenders aside: liquidity is not the test."""
    skipped = json.loads(SKIPPED.read_text(encoding="utf-8")) if SKIPPED.exists() else {}
    have = {r["sym"] for r in db.connect().execute("SELECT sym FROM stock")}
    rows = market.connect().execute("SELECT sym FROM class WHERE mcap >= ? ORDER BY mcap DESC", (min_mcap,)).fetchall()
    return [r[0] for r in rows if r[0] not in have and skipped.get(r[0]) not in ("financial company", "no quarters")]


def stage1(top, syms=None):
    con = db.connect()
    syms = syms if syms is not None else candidates(top)
    log(f"stage 1: {len(syms)} stocks, most liquid first, {WORKERS} workers")
    skipped, done, t0 = {}, 0, time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(yahoo_stage, s): s for s in syms}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                _, prof, rows, why = f.result()
            except Exception as e:
                skipped[sym] = f"{type(e).__name__}"
                continue
            if prof is None:
                skipped[sym] = why
                continue
            con.execute("INSERT OR REPLACE INTO stock(sym,name,sector,shares_cr,cap_employed,equity,debt,np_annual,updated,desk) VALUES(?,?,?,?,?,?,?,?,?,0)",
                        (sym, prof["name"], prof["industry"], prof["shares_cr"], prof["cap_employed"], prof["equity"], prof["debt"], prof["np_annual"],
                         datetime.now().isoformat(timespec="seconds")))
            stitch.apply_rows(sym, rows, "", con)
            done += 1
            if done % 25 == 0:
                con.commit()
                el = time.time() - t0
                log(f"  {done} added, {len(skipped)} skipped | {el / 60:.1f} min elapsed, ~{(len(syms) - done - len(skipped)) * el / max(done + len(skipped), 1) / 60:.0f} min left")
    con.commit()
    old = json.loads(SKIPPED.read_text(encoding="utf-8")) if SKIPPED.exists() else {}
    old.update(skipped)                                       # earlier runs' reasons are kept: the filings route reads them
    for sym in syms:
        if sym not in skipped:
            old.pop(sym, None)
    SKIPPED.write_text(json.dumps(old, indent=1), encoding="utf-8")
    from collections import Counter
    log(f"stage 1 done: {done} added, {len(skipped)} skipped {dict(Counter(skipped.values()).most_common(5))}")


def stage2():
    con = db.connect()
    todo = [r["sym"] for r in con.execute(
        "SELECT sym FROM stock WHERE desk=0 AND sym NOT IN (SELECT DISTINCT sym FROM quarter WHERE source='NSE XBRL') ORDER BY sym")]
    log(f"stage 2: NSE history for {len(todo)} screener-only stocks, 3 workers")
    done, none, t0 = 0, 0, time.time()
    with ThreadPoolExecutor(3) as ex:
        futs = {ex.submit(history_stage, s): s for s in todo}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                _, qs, scrip, rows, why = f.result()
            except Exception as e:
                log(f"  {sym}: {type(e).__name__}")
                continue
            if qs:
                con.executemany("INSERT OR REPLACE INTO quarter(sym,qend,sales,op,np,eps,basis,source,filed,ref,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                [(sym, q["qend"], q["sales"], q["op"], q["np"], q["eps"], q["basis"], q["source"], q["filed"], q["ref"], "")
                                 for q in qs])
                con.execute("UPDATE stock SET scrip=? WHERE sym=?", (scrip, sym))
                stitch.apply_rows(sym, rows, why, con)           # re-check Yahoo against the official history
                done += 1
            else:
                none += 1
            if (done + none) % 20 == 0:
                con.commit()
                el = time.time() - t0
                log(f"  {done} with history, {none} without | {el / 60:.1f} min elapsed, ~{(len(todo) - done - none) * el / max(done + none, 1) / 60:.0f} min left")
    con.commit()
    log(f"stage 2 done: {done} stocks got NSE history, {none} had none")


def filings_stage(sym):
    """For a stock Yahoo has no quarterly figures for: NSE's official history (to Dec 2024), then the newer quarters read from the results PDFs."""
    from collections import Counter
    prof = retry(lambda: yahoo.profile(sym))
    qs, scrip = retry(lambda: nse.fetch_quarters(sym))
    if not qs:
        return sym, prof, [], scrip
    for q in qs:
        q.setdefault("flags", [])
    basis = Counter(q["basis"] for q in qs[-8:]).most_common(1)[0][0]
    newer = []
    if scrip:
        newer = bse.fetch_missing(scrip, qs[-1]["qend"], {q["qend"]: q for q in qs}, sym=sym, basis=basis, last_sales=qs[-1]["sales"], log=lambda *a: None)
    return sym, prof, qs + newer, scrip


def stage_filings(syms, workers=3):
    """Screener-only stocks that Yahoo cannot serve, built from the exchanges' own filings."""
    from .sources import bse
    con = db.connect()
    log(f"filings route: {len(syms)} stocks, {workers} workers")
    done = none = 0
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(filings_stage, s): s for s in syms}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                _, prof, qs, scrip = f.result()
            except Exception as e:
                log(f"  {sym}: {type(e).__name__}: {e}")
                none += 1
                continue
            clean = [q for q in qs if not q["flags"]]
            if len(clean) < 5:
                none += 1
                log(f"  {sym}: only {len(clean)} usable quarters")
                continue
            con.execute("INSERT OR REPLACE INTO stock(sym,name,sector,shares_cr,cap_employed,equity,debt,np_annual,updated,desk,scrip) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                        (sym, prof["name"], prof["industry"], prof["shares_cr"], prof["cap_employed"], prof["equity"], prof["debt"], prof["np_annual"],
                         datetime.now().isoformat(timespec="seconds"), scrip))
            con.executemany("INSERT OR REPLACE INTO quarter(sym,qend,sales,op,np,eps,basis,source,filed,ref,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            [(sym, q["qend"], q["sales"], q["op"], q["np"], q.get("eps"), q["basis"], q["source"], q["filed"], q["ref"], "; ".join(q["flags"])) for q in qs])
            con.commit()
            done += 1
            if (done + none) % 10 == 0:
                el = time.time() - t0
                log(f"  {done} added, {none} not usable | {el / 60:.1f} min elapsed, ~{(len(syms) - done - none) * el / max(done + none, 1) / 60:.0f} min left")
    log(f"filings route done: {done} added, {none} not usable")


def refresh_yahoo():
    """Weekly upkeep for the screener-only stocks: re-fetch Yahoo's latest quarters and profile. Their NSE
    history is frozen (NSE's feed stops at Dec 2024), so it is never fetched again."""
    con = db.connect()
    syms = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE desk=0 AND COALESCE(kind,'corp')='corp'")]
    ok = bad = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(yahoo_stage, s): s for s in syms}
        for f in as_completed(futs):
            try:
                sym, prof, rows, why = f.result()
            except Exception:
                bad += 1
                continue
            if prof is None:
                bad += 1
                continue
            con.execute("UPDATE stock SET shares_cr=?, cap_employed=?, equity=?, debt=?, np_annual=?, updated=? WHERE sym=?",
                        (prof["shares_cr"], prof["cap_employed"], prof["equity"], prof["debt"], prof["np_annual"], datetime.now().isoformat(timespec="seconds"), sym))
            stitch.apply_rows(sym, rows, "", con)
            ok += 1
    con.commit()
    return ok, bad


def backfill_debt(log=print):
    """One pass over every stock that has no debt figure yet: Yahoo's latest balance sheet gives total debt (and refreshes equity)."""
    import pandas as pd
    con = db.connect()
    syms = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE debt IS NULL AND COALESCE(kind,'corp')='corp' ORDER BY sym")]
    log(f"debt for {len(syms)} stocks, {WORKERS} at a time")

    def one(sym):
        tk = yf.Ticker(sym + ".NS")
        bs = retry(lambda: tk.balance_sheet)
        if bs is None or bs.empty:
            return sym, None, None
        col = bs.columns[0]
        g = lambda *ns: next((float(bs.loc[n, col]) / 1e7 for n in ns if n in bs.index and not pd.isna(bs.loc[n, col])), None)
        return sym, g("Total Debt"), g("Stockholders Equity", "Common Stock Equity")
    done = none = 0
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for f in as_completed([ex.submit(one, s) for s in syms]):
            try:
                sym, debt, eq = f.result()
            except Exception:
                none += 1
                continue
            if debt is None:
                none += 1
                continue
            con.execute("UPDATE stock SET debt=?, equity=COALESCE(equity, ?) WHERE sym=?", (debt, eq, sym))
            done += 1
            if done % 100 == 0:
                con.commit()
                log(f"  {done} done, {none} without a balance sheet ({(time.time() - t0) / 60:.1f} min)")
    con.commit()
    log(f"debt stored for {done} stocks; {none} have no balance sheet at Yahoo")


def backfill_annual_profit(log=print):
    """One pass over every stock (banks and lenders too) that has no annual-profit figures yet."""
    con = db.connect()
    syms = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE np_annual IS NULL ORDER BY sym")]
    log(f"annual profit for {len(syms)} stocks, {WORKERS} at a time")
    done = none = 0
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(lambda s: (s, retry(lambda: yahoo.annual_profit(yf.Ticker(s + ".NS")))), sym): sym for sym in syms}
        for f in as_completed(futs):
            try:
                sym, v = f.result()
            except Exception:
                none += 1
                continue
            if not v:
                none += 1
                continue
            con.execute("UPDATE stock SET np_annual=? WHERE sym=?", (v, sym))
            done += 1
            if done % 200 == 0:
                con.commit()
                log(f"  {done} done ({(time.time() - t0) / 60:.1f} min)")
    con.commit()
    log(f"annual profit stored for {done}; {none} have none at Yahoo")


if __name__ == "__main__":
    if "--mcap" in sys.argv:                                  # python -m backend.widen --mcap 500  (then --history, then --annual and --debt)
        stage1(0, candidates_mcap(float(sys.argv[sys.argv.index("--mcap") + 1])))
    elif "--filings-syms" in sys.argv:                        # python -m backend.widen --filings-syms AEGISLOG,VRLLOG  (just these, even if already held)
        stage_filings(sys.argv[sys.argv.index("--filings-syms") + 1].split(","))
    elif "--filings" in sys.argv:                             # python -m backend.widen --filings 500
        mc = float(sys.argv[sys.argv.index("--filings") + 1])
        sk = json.loads(SKIPPED.read_text(encoding="utf-8"))
        have = {r["sym"] for r in db.connect().execute("SELECT sym FROM stock")}
        cap = dict(market.connect().execute("SELECT sym, mcap FROM class").fetchall())
        todo = sorted((s for s, why in sk.items() if why in ("no quarters", "no Yahoo quarters") and s not in have and (cap.get(s) or 0) >= mc), key=lambda s: -(cap.get(s) or 0))
        stage_filings(todo)
    elif "--annual" in sys.argv:
        backfill_annual_profit()
    elif "--debt" in sys.argv:
        backfill_debt()
    elif "--history" in sys.argv:
        stage2()
    else:
        top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 500
        stage1(top)
