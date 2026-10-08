"""Nightly refresh, meant to run unattended after the market closes.

    python -m backend.nightly              # prices for the whole market + the Desk's price charts (about 6 min)
    python -m backend.nightly --weekly     # the above, plus fundamentals for every Desk stock (about an hour)

What each step does
  1. market   reload every NSE stock's adjusted history (so splits and dividends stay consistent), drop
              unfinished and filler bars, patch Yahoo's gaps from NSE's own end-of-day file
  2. desk     copy the cleaned prices of the Desk's stocks into its chart table (same numbers as the screener)
  3. funds    (weekly) NSE results history + Yahoo's recent quarters, checked against any PDF-parsed quarters
  4. check    health checks; the verdict goes to data/nightly_status.json and the log

Safe to run unattended: one run at a time (lock file), nothing is deleted before its replacement has been
downloaded and sanity-checked, and a failed night leaves the previous data in place, flagged as stale.
Exit code 0 = healthy, 1 = finished with warnings, 2 = a step failed.
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import db, disclosures, financials, market, refresh, stitch, widen

DATA = Path(__file__).resolve().parent.parent / "data"
LOCK = DATA / "nightly.lock"
LOG = DATA / "nightly.log"
STATUS = DATA / "nightly_status.json"
STALE_LOCK_SECONDS = 4 * 3600
MIN_STOCKS = 1700            # fewer loaded stocks than this means Yahoo or NSE misbehaved
MAX_LOG_BYTES = 2_000_000


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    DATA.mkdir(exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:       # keep the last megabyte
        LOG.write_text(LOG.read_text(encoding="utf-8", errors="ignore")[-1_000_000:], encoding="utf-8")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def take_lock():
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < STALE_LOCK_SECONDS:
            return False
        log(f"removing a stale lock ({age / 3600:.1f} h old)")
    LOCK.write_text(str(os.getpid()))
    return True


def step_market(report):
    loaded, skipped = market.load(log=log)
    report["stocks_loaded"], report["stocks_skipped"] = loaded, skipped
    return f"{loaded} stocks loaded, {skipped} without data"


def step_extras(report):
    """Delivery % and the ASM surveillance list: small NSE feeds that change every day."""
    con = market.connect()
    n = market.load_delivery(con, log=log)
    a = market.load_asm(con, log=log)
    report["delivery_rows_added"], report["asm_stocks"] = n, a
    return f"{n} delivery rows, {a} ASM stocks"


def step_desk(report):
    """The Desk draws its charts from the `bar` table; feed it from market.db so both show the same prices."""
    con, mcon = db.connect(), market.connect()
    n = 0
    for (sym,) in con.execute("SELECT sym FROM stock").fetchall():
        rows = mcon.execute("SELECT sym,d,o,h,l,c,v FROM mbar WHERE sym=? ORDER BY d", (sym,)).fetchall()
        if len(rows) < 200:                    # not in the market database (or too short): leave its bars alone
            continue
        con.execute("DELETE FROM bar WHERE sym=?", (sym,))
        con.executemany("INSERT INTO bar(sym,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)", [(r[0], r[1], r[2], r[3], r[4], r[5], int(r[6])) for r in rows])
        n += 1
    con.commit()
    report["desk_stocks_synced"] = n
    return f"{n} Desk stocks synced"


def step_screens(report):
    """Run every saved screen on the new data and remember who matched, so the screener can show what is new."""
    from . import fundamentals, screens
    from . import indicators as ind
    con = db.connect()
    if not con.execute("SELECT 1 FROM screen LIMIT 1").fetchone():
        return "no saved screens"
    from . import industries
    data = ind.load()
    n = screens.snapshot_all(con, industries.enrich(ind.snapshot(data), data), fundamentals.snapshot())
    report["screens_snapshotted"] = n
    return f"{n} saved screens snapshotted"


def step_classify(report):
    """New listings get their industry classification; every stock's market cap is refreshed (feeds the Industries page)."""
    from . import classify
    total = classify.run(log=log)
    report["classified"] = total
    return f"{total} stocks classified"


def step_disclosures(report):
    """Insider trades, promoter holding and pledge: daily for the Desk's stocks, weekly (resumable) for all of them."""
    con = db.connect()
    syms = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE desk=1 ORDER BY sym")]
    ok, bad = disclosures.refresh(syms, max_age_days=0, log=log)
    if report.get("weekly"):
        rest = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE desk=0 ORDER BY sym")]
        ok2, bad2 = disclosures.refresh(rest, max_age_days=5, log=log)
        ok, bad = ok + ok2, bad + bad2
    report["disclosures_ok"], report["disclosures_failed"] = ok, bad
    return f"{ok} stocks refreshed, {bad} failed"


def step_funds(report):
    con = db.connect()
    syms = [(r["sym"], r["sector"]) for r in con.execute("SELECT sym, sector FROM stock WHERE desk=1 ORDER BY sym")]
    ok = bad = 0
    for sym, sector in syms:
        try:
            refresh.refresh(sym, sector, con, pdf=False)
            stitch.stitch(sym, con)
            ok += 1
        except Exception as e:                  # one stock failing must not stop the other 100
            bad += 1
            log(f"  fundamentals failed for {sym}: {type(e).__name__}: {e}")
    # the screener-only stocks: Yahoo's latest quarters only (their NSE history is frozen), a few minutes for ~1,000
    ok2, bad2 = widen.refresh_yahoo()
    ok3, bad3 = financials.refresh_all()                    # banks, lenders, insurers: their own Yahoo mapping
    report["financials_refreshed"], report["financials_failed"] = ok3, bad3
    ok2, bad2 = ok2 + ok3, bad2 + bad3
    report["fundamentals_ok"], report["fundamentals_failed"] = ok + ok2, bad + bad2
    report["screener_only_refreshed"], report["screener_only_skipped"] = ok2, bad2
    return f"{ok} Desk stocks + {ok2} screener-only stocks refreshed, {bad + bad2} failed or skipped"


def step_standalone(report, workers=2):
    """Standalone figures for the Desk's companies, so the Consolidated / Standalone switch is instant. Each stock takes up to a minute the first time
    (the filings are read one by one) and is then kept for three days, so a weekly run only redoes what has expired."""
    from concurrent.futures import ThreadPoolExecutor
    from . import company
    syms = [r["sym"] for r in db.connect().execute("SELECT sym FROM stock WHERE desk=1 AND COALESCE(kind,'')!='fin' ORDER BY sym")]

    def one(sym):
        try:
            return bool(company.standalone(sym)["has_both"])
        except Exception as e:
            log(f"  standalone failed for {sym}: {type(e).__name__}: {e}")
            return None
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(one, syms))
    both, failed = sum(r is True for r in res), sum(r is None for r in res)
    report["standalone_both"], report["standalone_failed"] = both, failed
    return f"{len(syms)} Desk stocks: {both} have a separate standalone filing, {failed} failed"


def health(report):
    """Checks that catch a quietly bad night. Returns a list of warnings."""
    w = []
    con = market.connect()
    last = market.last_complete_date()
    newest = con.execute("SELECT MAX(d) FROM mbar").fetchone()[0]
    report["newest_session"], report["expected_session"] = newest, last
    if newest != last:
        w.append(f"newest session in the database is {newest}, expected {last}"
                 + (" (a market holiday?)" if newest and newest > last else ""))
    counts = [r[0] for r in con.execute("SELECT COUNT(*) FROM mbar WHERE d >= date(?, '-30 day') GROUP BY d ORDER BY d", (last,))]
    if counts:
        typical = sorted(counts)[len(counts) // 2]
        report["bars_on_newest"] = counts[-1]
        if counts[-1] < 0.8 * typical:
            w.append(f"only {counts[-1]} stocks have a bar on the newest session (typical {typical})")
    if report.get("stocks_loaded", MIN_STOCKS) < MIN_STOCKS:
        w.append(f"only {report['stocks_loaded']} stocks loaded (expected 1,700+)")
    n_idx = con.execute("SELECT COUNT(*), MAX(d) FROM idx").fetchone()
    if n_idx[0] < 200 or n_idx[1] != last:
        w.append(f"Nifty 500 benchmark: {n_idx[0]} bars, newest {n_idx[1]}")
    if report.get("fundamentals_failed"):
        w.append(f"{report['fundamentals_failed']} stocks failed the fundamentals refresh")
    return w


def main(argv):
    weekly = "--weekly" in argv
    DATA.mkdir(exist_ok=True)
    if not take_lock():
        log("another nightly run is still in progress; exiting")
        return 1
    report = {"started": datetime.now().isoformat(timespec="seconds"), "weekly": weekly, "steps": [], "ok": False}
    code = 0
    try:
        steps = ([("market", step_market)] if "--skip-market" not in argv else []) + [("extras", step_extras), ("desk", step_desk), ("classify", step_classify), ("screens", step_screens), ("disclosures", step_disclosures)]
        if weekly:
            steps.append(("fundamentals", step_funds))
            steps.append(("standalone", step_standalone))
        if "--only" in argv:                                 # a top-up run: just these steps, e.g. --only extras,desk,screens
            keep = set(argv[argv.index("--only") + 1].split(","))
            steps = [st for st in steps if st[0] in keep]
            report["only"] = sorted(keep)
        for name, fn in steps:
            t = time.time()
            log(f"step {name}: start")
            try:
                detail = fn(report)
                report["steps"].append({"name": name, "ok": True, "seconds": round(time.time() - t), "detail": detail})
                log(f"step {name}: done in {time.time() - t:.0f}s - {detail}")
            except Exception as e:
                code = 2
                report["steps"].append({"name": name, "ok": False, "seconds": round(time.time() - t), "detail": f"{type(e).__name__}: {e}"})
                log(f"step {name}: FAILED - {type(e).__name__}: {e}\n{traceback.format_exc()}")
                if name == "market":            # nothing else is worth doing on yesterday's market data
                    break
        warnings = health(report)
        report["warnings"] = warnings
        for w in warnings:
            log(f"WARNING: {w}")
        if code == 0 and warnings:
            code = 1
        report["ok"] = code == 0
    finally:
        report["finished"] = datetime.now().isoformat(timespec="seconds")
        report["exit_code"] = code
        STATUS.write_text(json.dumps(report, indent=1), encoding="utf-8")
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass
    log(f"finished: exit code {code} ({'healthy' if code == 0 else 'warnings' if code == 1 else 'FAILED'})")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
