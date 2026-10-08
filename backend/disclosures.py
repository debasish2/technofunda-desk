"""Insider trades, promoter holding and promoter pledge, from NSE's own disclosure feeds.

    python -m backend.disclosures --desk       # the Desk's stocks first (a few minutes)
    python -m backend.disclosures --all        # every stock in the database (about an hour); resumable

Three feeds per stock:
  corporates-pit                    insider trades (SEBI PIT regulations): who, mode, shares, value, date
  corporate-share-holdings-master   quarterly shareholding patterns, from which promoter holding over time
  corporate-pledgedata              the latest promoter pledge: shares pledged against promoter shares and all shares

Insider rows are classified so the totals mean something:
  Buy / Sell   open-market purchases and sales only (a real decision to put money in or take it out)
  Other        everything else (off-market and inter-se transfers, gifts, ESOP exercises, pledges and revocations);
               listed for completeness, never counted in "bought" or "sold"
A stock with no filings in the window is recorded as fetched, so "none" is distinguishable from "not loaded yet".
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import requests

from . import db

API = "https://www.nseindia.com/api/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
      "Accept": "application/json", "Referer": "https://www.nseindia.com/"}
WORKERS = 3
WINDOW_DAYS = 730
_local = threading.local()


def session():
    """One NSE session (with its cookie) per worker thread."""
    s = getattr(_local, "s", None)
    if s is None or time.time() - _local.t > 600:           # NSE cookies go stale
        s = requests.Session()
        s.headers.update(UA)
        s.get("https://www.nseindia.com/", timeout=20)
        _local.s, _local.t = s, time.time()
    return s


def get(path, **params):
    for i in range(3):
        try:
            r = session().get(API + path, params=params, timeout=30)
            if r.status_code == 200:
                time.sleep(0.25)
                return r.json()
            _local.s = None
        except Exception:
            _local.s = None
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"NSE {path} failed for {params.get('symbol')}")


def d_iso(txt):
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(txt.strip().title(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            pass
    return None


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def classify(txn, mode):
    txn, mode = (txn or "").lower(), (mode or "").lower()
    if "market purchase" in mode and "off" not in mode:
        return "Buy"
    if "market sale" in mode and "off" not in mode:
        return "Sell"
    return "Other"


def fetch(sym):
    """Worker thread: returns the three feeds' parsed rows for one stock."""
    frm = (date.today() - timedelta(days=WINDOW_DAYS)).strftime("%d-%m-%Y")
    pit = get("corporates-pit", index="equities", symbol=sym, from_date=frm, to_date=date.today().strftime("%d-%m-%Y")).get("data", [])
    hold = get("corporate-share-holdings-master", index="equities", symbol=sym)
    pledge = get("corporate-pledgedata", index="equities", symbol=sym).get("data", [])
    ins = []
    for x in pit:
        d = d_iso(x.get("acqfromDt") or "") or d_iso(x.get("date") or "")
        qty, val = num(x.get("secAcq")), num(x.get("secVal"))
        if not d or qty is None:
            continue
        ins.append((sym, str(x.get("did") or x.get("pid") or f"{d}-{x.get('acqName')}-{qty}"), d, (x.get("acqName") or "").strip().title(),
                    x.get("personCategory") or "", x.get("tdpTransactionType") or "", x.get("acqMode") or "",
                    classify(x.get("tdpTransactionType"), x.get("acqMode")), qty, round((val or 0) / 1e7, 4), num(x.get("afterAcqSharesPer"))))
    hold_rows = []
    for x in hold if isinstance(hold, list) else []:
        d = d_iso(x.get("date") or "")
        if not d:
            continue
        dd = date.fromisoformat(d)
        if dd.month in (3, 6, 9, 12) and (dd + timedelta(days=1)).day == 1 and num(x.get("pr_and_prgrp")) is not None:
            hold_rows.append((sym, d, num(x["pr_and_prgrp"]), num(x.get("public_val"))))
    pl = None
    if pledge:
        p = pledge[0]
        asof, ph, pshares, tot = d_iso(p.get("shp") or ""), num(p.get("percPromoterHolding")), num(p.get("numSharesPledged")), num(p.get("totPromoterHolding"))
        if asof:
            pl = (sym, asof, ph, pshares, tot, round(pshares / tot * 100, 2) if (pshares is not None and tot) else None, num(p.get("percSharesPledged")))
    return sym, ins, hold_rows, pl


def store(res, con):
    sym, ins, hold, pl = res
    con.execute("DELETE FROM insider WHERE sym=?", (sym,))                   # the feed returns the full window each time
    con.executemany("INSERT OR REPLACE INTO insider VALUES(?,?,?,?,?,?,?,?,?,?,?)", ins)
    con.executemany("INSERT OR REPLACE INTO holding VALUES(?,?,?,?)", hold)
    if pl:
        con.execute("INSERT OR REPLACE INTO pledge VALUES(?,?,?,?,?,?,?)", pl)
    con.execute("INSERT OR REPLACE INTO disc_fetch VALUES(?,?)", (sym, datetime.now().isoformat(timespec="seconds")))


def refresh(syms, max_age_days=0, log=print):
    """Fetch and store the given stocks. Stocks fetched within `max_age_days` are skipped (resumable)."""
    con = db.connect()
    fresh = {r["sym"] for r in con.execute("SELECT sym FROM disc_fetch WHERE fetched >= ?",
                                           ((datetime.now() - timedelta(days=max_age_days)).isoformat(),))} if max_age_days else set()
    todo = [s for s in syms if s not in fresh]
    log(f"disclosures: {len(todo)} stocks to fetch ({len(syms) - len(todo)} fresh), {WORKERS} workers")
    ok = bad = 0
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch, s): s for s in todo}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                store(f.result(), con)
                ok += 1
            except Exception as e:
                bad += 1
                log(f"  {futs[f]}: {type(e).__name__}")
            if i % 50 == 0:
                con.commit()
                log(f"  {i}/{len(todo)} | {(time.time() - t0) / 60:.1f} min")
    con.commit()
    log(f"disclosures done: {ok} stored, {bad} failed")
    return ok, bad


if __name__ == "__main__":
    con = db.connect()
    if "--all" in sys.argv:
        syms = [r["sym"] for r in con.execute("SELECT sym FROM stock ORDER BY desk DESC, sym")]
    else:
        syms = [r["sym"] for r in con.execute("SELECT sym FROM stock WHERE desk=1 ORDER BY sym")]
    refresh(syms, max_age_days=5 if "--all" in sys.argv else 0)
