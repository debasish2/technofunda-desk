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
from .sources import nse, yahoo

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


def stage1(top):
    con = db.connect()
    syms = candidates(top)
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
            con.execute("INSERT OR REPLACE INTO stock(sym,name,sector,shares_cr,cap_employed,equity,updated,desk) VALUES(?,?,?,?,?,?,?,0)",
                        (sym, prof["name"], prof["industry"], prof["shares_cr"], prof["cap_employed"], prof["equity"],
                         datetime.now().isoformat(timespec="seconds")))
            stitch.apply_rows(sym, rows, "", con)
            done += 1
            if done % 25 == 0:
                con.commit()
                el = time.time() - t0
                log(f"  {done} added, {len(skipped)} skipped | {el / 60:.1f} min elapsed, ~{(len(syms) - done - len(skipped)) * el / max(done + len(skipped), 1) / 60:.0f} min left")
    con.commit()
    SKIPPED.write_text(json.dumps(skipped, indent=1), encoding="utf-8")
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
            con.execute("UPDATE stock SET shares_cr=?, cap_employed=?, equity=?, updated=? WHERE sym=?",
                        (prof["shares_cr"], prof["cap_employed"], prof["equity"], datetime.now().isoformat(timespec="seconds"), sym))
            stitch.apply_rows(sym, rows, "", con)
            ok += 1
    con.commit()
    return ok, bad


if __name__ == "__main__":
    if "--history" in sys.argv:
        stage2()
    else:
        top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 500
        stage1(top)
