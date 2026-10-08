"""Intraday refresh for the breadth numbers.

    python -m backend.live            # one refresh, only while NSE is open (09:15-16:35 IST, Mon-Fri)
    python -m backend.live --force    # refresh now whatever the clock says

The end-of-day load (backend.nightly) is what the screener and the charts rely on. This job only adds a provisional "today" row for
the market-wide breadth: it asks Yahoo for today's running bar of every stock that traded on the last session (prices are about 15
minutes late) and keeps it in market.db's live_bar table, with its time in the meta table. The breadth page and the Desk's
market-tone badge append that row to the stored history while it is newer than the last full session. Nothing in the end-of-day
tables is touched, and the next nightly load makes the row irrelevant.
"""
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from . import market

IST = ZoneInfo("Asia/Kolkata")


LOG = __import__("pathlib").Path(__file__).resolve().parent.parent / "data" / "live.log"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:                 # the scheduled task has no console, so keep a log
            f.write(line + "\n")
    except OSError:
        pass


def market_hours(now=None):
    now = now or datetime.now(IST)
    return now.weekday() < 5 and (9 * 60 + 15) <= now.hour * 60 + now.minute <= (16 * 60 + 35)


def refresh(force=False, log=log):
    now = datetime.now(IST)
    if not force and not market_hours(now):
        log("NSE is closed; nothing to do")
        return None
    today = now.strftime("%Y-%m-%d")
    con = market.connect()
    last = con.execute("SELECT MAX(d) FROM mbar").fetchone()[0]
    if last is None or last >= today:
        log(f"the end-of-day load already has {last}; no live row needed")
        return None
    syms = [r[0] for r in con.execute("SELECT sym FROM mbar WHERE d=?", (last,))]
    rows, t0 = [], time.time()
    for i in range(0, len(syms), market.CHUNK):
        part = syms[i:i + market.CHUNK]
        try:
            d = yf.download([s + ".NS" for s in part], period="5d", interval="1d", auto_adjust=True, group_by="ticker",
                            threads=True, progress=False)
        except Exception as e:
            log(f"chunk {i // market.CHUNK}: failed ({type(e).__name__}); skipped")
            continue
        keys = set(d.columns.get_level_values(0))
        for s in part:
            if s + ".NS" not in keys:
                continue
            x = d[s + ".NS"].dropna(subset=["Close"])
            if x.empty or x.index[-1].strftime("%Y-%m-%d") != today:
                continue
            r = x.iloc[-1]
            if min(r.Open, r.High, r.Low, r.Close) <= 0 or r.High < r.Low:
                continue
            rows.append((s, today, float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume or 0)))
    ix = yf.download(market.INDEX, period="5d", interval="1d", progress=False, auto_adjust=True)["Close"].squeeze().dropna()
    idx_today = float(ix.iloc[-1]) if len(ix) and ix.index[-1].strftime("%Y-%m-%d") == today else None
    if len(rows) < 0.8 * len(syms) or idx_today is None:
        log(f"only {len(rows)} of {len(syms)} stocks have a bar for {today} so far (index: {idx_today}); keeping the previous snapshot")
        return None
    with con:
        con.execute("DELETE FROM live_bar")
        con.executemany("INSERT INTO live_bar VALUES(?,?,?,?,?,?,?)", rows)
        for k, v in (("live_d", today), ("live_asof", now.strftime("%Y-%m-%dT%H:%M:%S")), ("live_n", str(len(rows))), ("live_idx", str(idx_today))):
            con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    log(f"live snapshot: {len(rows)} of {len(syms)} stocks, Nifty 500 {idx_today:,.1f} ({time.time() - t0:.0f}s)")
    return len(rows)


if __name__ == "__main__":
    refresh(force="--force" in sys.argv)
