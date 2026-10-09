"""Full-market price database for breadth and technical screening.

    python -m backend.market            # reload every NSE equity (EQ + BE series) + the Nifty 500 index
    python -m backend.market --repair   # no reload: clean the stored bars and patch missing sessions

Kept in its own file (data/market.db) so the long fundamentals refresh and this load never fight over
one SQLite writer. Prices are split/bonus-adjusted (Yahoo), which matters: raw prices would make an
EMA or a 52-week high jump on every corporate action.

Three kinds of bad rows are kept out, because breadth counts every stock on every day:
  * zero-volume bars (Yahoo fills market holidays with flat copies of the last close)
  * today's bar while the market is open (a 15-minute-old candle is not a session)
  * sessions where Yahoo has data for only a fraction of stocks: patched from NSE's own end-of-day file
"""
import csv
import io
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

MARKET_DB = Path(__file__).resolve().parent.parent / "data" / "market.db"
UNIVERSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dmy}.csv"
INDEX = "^CRSLDX"            # Nifty 500
SERIES = ("EQ", "BE")        # BE = trade-to-trade segment (e.g. Sterlite, HFCL); still ordinary stocks
EXTRA_SERIES = ("SM", "ST", "IV", "RR")   # SME platform (normal / trade-to-trade), InvITs, REITs: kept only when they trade enough (MIN_EXTRA_VALUE)
SME_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
MIN_EXTRA_VALUE = 1_000_000  # median traded value over the last 20 sessions, rupees: below this an SME or InvIT stock is left out
CHUNK = 100
MIN_BARS = 60                # fewer bars than this and a stock is left out (new listings, suspended names)
IST = ZoneInfo("Asia/Kolkata")
CLOSE_TIME = (16, 0)         # a session is complete once the clock passes this (IST)

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (sym TEXT PRIMARY KEY, name TEXT, isin TEXT, series TEXT);
CREATE TABLE IF NOT EXISTS mbar (sym TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (sym, d));
CREATE TABLE IF NOT EXISTS idx (d TEXT PRIMARY KEY, c REAL);
CREATE TABLE IF NOT EXISTS live_bar (sym TEXT PRIMARY KEY, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS deliv (sym TEXT, d TEXT, pct REAL, PRIMARY KEY (sym, d));
CREATE TABLE IF NOT EXISTS asm (sym TEXT PRIMARY KEY, kind TEXT, stage TEXT, asof TEXT);
"""


def connect():
    MARKET_DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(MARKET_DB, timeout=60)
    con.executescript(SCHEMA)
    if "series" not in [r[1] for r in con.execute("PRAGMA table_info(universe)")]:
        con.execute("ALTER TABLE universe ADD COLUMN series TEXT")
    return con


def nse_equities():
    r = requests.get(UNIVERSE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    rows = csv.DictReader(io.StringIO(r.text))
    return [(x["SYMBOL"].strip(), x["NAME OF COMPANY"].strip(), x[" ISIN NUMBER"].strip(), x[" SERIES"].strip())
            for x in rows if x[" SERIES"].strip() in SERIES]


def bhav_rows(d):
    """NSE's end-of-day file for date `d` as dict rows, or None on a holiday / not published."""
    r = requests.get(BHAV_URL.format(dmy=d.strftime("%d%m%Y")), headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    if not r.ok or "SYMBOL" not in r.text[:40].upper():
        return None
    rows = list(csv.DictReader(io.StringIO(r.text.replace(" ", ""))))
    return rows if {x.get("DATE1", "").upper() for x in rows[:50]} == {d.strftime("%d-%b-%Y").upper()} else None


def nse_extras():
    """SME-platform stocks (names from NSE's SME list), InvITs and REITs (symbols from the latest end-of-day file, which has no names)."""
    out, seen = [], {u[0] for u in nse_equities()}
    r = requests.get(SME_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    if r.ok:
        for x in csv.DictReader(io.StringIO(r.text)):
            sym, ser = (x.get("SYMBOL") or "").strip(), (x.get("SERIES") or "").strip()
            if sym and ser in EXTRA_SERIES and sym not in seen:
                out.append((sym, (x.get("NAME_OF_COMPANY") or sym).strip(), (x.get("ISIN_NUMBER") or "").strip(), ser))
                seen.add(sym)
    d = date.fromisoformat(last_complete_date())
    for _ in range(6):
        rows = bhav_rows(d)
        if rows:
            for x in rows:
                if x["SERIES"] in ("IV", "RR") and x["SYMBOL"] not in seen:
                    out.append((x["SYMBOL"], x["SYMBOL"], "", x["SERIES"]))
                    seen.add(x["SYMBOL"])
            break
        d -= timedelta(days=1)
    return out


def bhav_history(con, syms, sessions=140, log=print):
    """Bars for stocks Yahoo does not carry, from NSE's own end-of-day files (raw prices: fine for recent listings)."""
    want, got, d, tried = set(syms), {}, date.fromisoformat(last_complete_date()), 0
    while tried < sessions:
        if d.weekday() < 5:
            tried += 1
            rows = bhav_rows(d)
            for x in rows or []:
                if x["SYMBOL"] in want:
                    try:
                        got.setdefault(x["SYMBOL"], []).append((x["SYMBOL"], d.isoformat(), *(float(x[k]) for k in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"))))
                    except ValueError:
                        pass
        d -= timedelta(days=1)
    n = 0
    for s, bars in got.items():
        if len(bars) >= MIN_BARS:
            con.executemany("INSERT OR REPLACE INTO mbar VALUES(?,?,?,?,?,?,?)", bars)
            n += 1
    con.commit()
    log(f"NSE files: bars for {n} of {len(want)} stocks Yahoo does not carry")
    return n


def cull_extras(con, log=print):
    """Drop SME / InvIT / REIT stocks that trade too little to be worth a chart or a screen, and those with too few bars."""
    keep = {}
    for s, in con.execute("SELECT sym FROM universe WHERE series IN ('SM','ST','IV','RR')").fetchall():
        v = [c * q for c, q in con.execute("SELECT c, v FROM mbar WHERE sym=? ORDER BY d DESC LIMIT 20", (s,))]
        n = con.execute("SELECT COUNT(*) FROM mbar WHERE sym=?", (s,)).fetchone()[0]
        keep[s] = n >= MIN_BARS and len(v) >= 10 and sorted(v)[len(v) // 2] >= MIN_EXTRA_VALUE
    drop = [s for s, k in keep.items() if not k]
    con.executemany("DELETE FROM mbar WHERE sym=?", [(s,) for s in drop])
    con.executemany("DELETE FROM universe WHERE sym=?", [(s,) for s in drop])
    con.commit()
    log(f"SME / InvIT / REIT: {len(keep) - len(drop)} kept, {len(drop)} left out (too few bars or too little trading)")


def last_complete_date(now=None):
    """Newest date whose session is over: today after the close, otherwise the previous weekday."""
    now = now or datetime.now(IST)
    d = now.date()
    if (now.hour, now.minute) < CLOSE_TIME or d.weekday() >= 5:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def clean(con):
    """Remove bars that are not real completed sessions. Returns (zero_volume, unfinished)."""
    z = con.execute("DELETE FROM mbar WHERE v = 0 OR v IS NULL").rowcount
    u = con.execute("DELETE FROM mbar WHERE d > ?", (last_complete_date(),)).rowcount
    con.execute("DELETE FROM idx WHERE d > ?", (last_complete_date(),))
    con.commit()
    return z, u


def patch_gaps(con, days=14, log=print):
    """For recent weekdays where far fewer stocks have a bar than normal, fill from NSE's end-of-day file."""
    last = last_complete_date()
    counts = dict(con.execute("SELECT d, COUNT(*) FROM mbar WHERE d >= ? GROUP BY d",
                              ((date.fromisoformat(last) - timedelta(days=days + 20)).isoformat(),)))
    typical = sorted(counts.values())[len(counts) // 2] if counts else 0
    uni = {r[0] for r in con.execute("SELECT sym FROM universe")}
    patched = 0
    d = date.fromisoformat(last)
    for _ in range(days):
        if d.weekday() < 5:
            iso = d.isoformat()
            if counts.get(iso, 0) < 0.8 * typical:
                n = _patch_day(con, d, uni, log)
                patched += n
        d -= timedelta(days=1)
    return patched


def _patch_day(con, d, uni, log):
    r = requests.get(BHAV_URL.format(dmy=d.strftime("%d%m%Y")), headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    if not r.ok or "SYMBOL" not in r.text[:40].upper():
        log(f"  {d}: no NSE end-of-day file (holiday or not published yet)")
        return 0
    rows = list(csv.DictReader(io.StringIO(r.text.replace(" ", ""))))
    # on a market holiday NSE re-serves the previous day's file under the holiday's name: check the date inside
    stamp = {x.get("DATE1", "").upper() for x in rows[:50]}
    if stamp != {d.strftime("%d-%b-%Y").upper()}:
        log(f"  {d}: NSE's file is dated {sorted(stamp)} - the market was closed; nothing patched")
        return 0
    iso = d.isoformat()
    have = {x[0] for x in con.execute("SELECT sym FROM mbar WHERE d=?", (iso,))}
    prev = dict(con.execute("SELECT sym, c FROM mbar WHERE d = (SELECT MAX(d) FROM mbar WHERE d < ?)", (iso,)))
    add, skipped = [], 0
    for x in rows:
        s = x["SYMBOL"]
        if s not in uni or x["SERIES"] not in SERIES + EXTRA_SERIES or s in have:
            continue
        try:
            o, h, l, c, v = (float(x[k]) for k in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"))
        except ValueError:
            continue
        if v <= 0 or (s in prev and not 0.75 <= c / prev[s] <= 1.25):
            skipped += 1                    # a gap this big is a split/bonus: the raw file would break the adjusted series
            continue
        add.append((s, iso, o, h, l, c, v))
    con.executemany("INSERT OR REPLACE INTO mbar VALUES(?,?,?,?,?,?,?)", add)
    con.commit()
    log(f"  {d}: patched {len(add)} bars from NSE's end-of-day file ({skipped} skipped as likely corporate actions)")
    return len(add)


def load(years=2, log=print, only=None):
    """Download adjusted history. `only` = list of symbols to (re)load without touching the rest."""
    con = connect()
    # fetch and sanity-check the inputs BEFORE touching the database, so a failed download on an
    # unattended night leaves yesterday's data intact instead of an empty universe or benchmark
    uni = nse_equities()
    try:
        extras = nse_extras()
    except Exception as e:                                       # the main universe must still load if the extra lists are unavailable
        extras = []
        log(f"SME / InvIT lists unavailable ({type(e).__name__}); loading the main universe only")
    uni += extras
    ix = yf.download(INDEX, period=f"{years}y", interval="1d", progress=False, auto_adjust=True)
    ix = ix["Close"].squeeze().dropna()
    if len(uni) - len(extras) < 1500:
        raise RuntimeError(f"NSE universe list has only {len(uni) - len(extras)} symbols; refusing to replace the stored one")
    if len(ix) < 200:
        raise RuntimeError(f"Nifty 500 download returned {len(ix)} bars; refusing to replace the stored benchmark")
    if only is None:
        con.execute("DELETE FROM universe")
    con.executemany("INSERT OR REPLACE INTO universe VALUES(?,?,?,?)", uni)
    con.execute("DELETE FROM idx")
    con.executemany("INSERT INTO idx VALUES(?,?)", [(i.strftime("%Y-%m-%d"), float(v)) for i, v in ix.items()])
    con.commit()
    log(f"index: {len(ix)} bars to {ix.index[-1].date()}; universe: {len(uni)} symbols")

    syms = [u[0] for u in uni if only is None or u[0] in only]
    loaded = skipped = 0
    for i in range(0, len(syms), CHUNK):
        part = syms[i:i + CHUNK]
        t = time.time()
        try:
            d = yf.download([s + ".NS" for s in part], period=f"{years}y", interval="1d", auto_adjust=True,
                            group_by="ticker", threads=True, progress=False)
        except Exception as e:                       # one bad chunk should not lose the rest
            log(f"chunk {i // CHUNK}: failed ({type(e).__name__}); skipped")
            continue
        rows = []
        for s in part:
            key = s + ".NS"
            if key not in d.columns.get_level_values(0):
                skipped += 1
                continue
            x = d[key].dropna(subset=["Close"])
            if len(x) < MIN_BARS:
                skipped += 1
                continue
            loaded += 1
            rows += [(s, ts.strftime("%Y-%m-%d"), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume))
                     for ts, r in x.iterrows()]
        con.executemany("INSERT OR REPLACE INTO mbar VALUES(?,?,?,?,?,?,?)", rows)
        con.commit()
        log(f"chunk {i // CHUNK + 1}/{(len(syms) + CHUNK - 1) // CHUNK}: {loaded} loaded, {skipped} skipped ({time.time() - t:.0f}s)")
    ex = [u[0] for u in extras if only is None or u[0] in only]
    if ex:
        have = {r[0] for r in con.execute("SELECT DISTINCT sym FROM mbar")}
        miss = [s for s in ex if s not in have]
        if miss:
            bhav_history(con, miss, log=log)
        cull_extras(con, log)
    return repair(con, log) and (loaded, skipped)


def load_delivery(con=None, days=14, log=print):
    """Delivery % per stock from NSE's end-of-day file, for recent sessions not stored yet. Trade-to-trade (BE) stocks have
    none (every trade there is a delivery trade), so they are left blank rather than shown as 0."""
    con = con or connect()
    last = date.fromisoformat(last_complete_date())
    have = {r[0] for r in con.execute("SELECT DISTINCT d FROM deliv")}
    added = 0
    d = last
    for _ in range(days):
        iso = d.isoformat()
        if d.weekday() < 5 and iso not in have:
            r = requests.get(BHAV_URL.format(dmy=d.strftime("%d%m%Y")), headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            if r.ok and "SYMBOL" in r.text[:40].upper():
                rows = list(csv.DictReader(io.StringIO(r.text.replace(" ", ""))))
                if {x.get("DATE1", "").upper() for x in rows[:50]} == {d.strftime("%d-%b-%Y").upper()}:     # not a holiday re-serve
                    out = []
                    for x in rows:
                        try:
                            out.append((x["SYMBOL"], iso, float(x["DELIV_PER"])))
                        except (ValueError, KeyError):
                            pass
                    con.executemany("INSERT OR REPLACE INTO deliv VALUES(?,?,?)", out)
                    added += len(out)
        d -= timedelta(days=1)
    con.commit()
    log(f"delivery: {added} stock-days added")
    return added


def load_asm(con=None, log=print):
    """NSE's Additional Surveillance Measure list (long-term and short-term, with stage), replacing the stored one."""
    con = con or connect()
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
                      "Accept": "application/json", "Referer": "https://www.nseindia.com/"})
    s.get("https://www.nseindia.com/", timeout=20)
    d = s.get("https://www.nseindia.com/api/reportASM", timeout=30).json()
    rows = []
    for kind, label in (("longterm", "Long term"), ("shortterm", "Short term")):
        for x in (d.get(kind) or {}).get("data", []):
            rows.append((x["symbol"], label, x.get("asmSurvIndicator") or "", x.get("asmTime") or ""))
    if len(rows) < 20:                                                   # an empty answer must not wipe the list
        raise RuntimeError(f"ASM feed returned only {len(rows)} rows")
    con.execute("DELETE FROM asm")
    con.executemany("INSERT OR REPLACE INTO asm VALUES(?,?,?,?)", rows)
    con.commit()
    log(f"ASM: {len(rows)} stocks under additional surveillance")
    return len(rows)


def repair(con=None, log=print):
    con = con or connect()
    z, u = clean(con)
    log(f"cleaned: {z} zero-volume bars, {u} bars from an unfinished session")
    n = patch_gaps(con, log=log)
    con.execute("INSERT OR REPLACE INTO meta VALUES('loaded', datetime('now'))")
    con.commit()
    return True


if __name__ == "__main__":
    if "--repair" in sys.argv:
        repair()
    elif "--extras" in sys.argv:                    # only the SME / InvIT / REIT names: python -m backend.market --extras
        print(load(only={u[0] for u in nse_extras()}))
    elif "--be" in sys.argv:                        # only the BE-series names that an earlier load did not have
        con = connect()
        have = {r[0] for r in con.execute("SELECT DISTINCT sym FROM mbar")}
        print(load(only={u[0] for u in nse_equities() if u[3] == "BE"} - have))
    else:
        print(load())
