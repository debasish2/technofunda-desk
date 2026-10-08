"""Full price history for the chart.

The screener works on two years of bars (data/market.db) and the Desk database keeps four, which is plenty for ranks and
indicators but not for looking at a stock's whole life. This module fetches a stock's entire Yahoo history on demand
(split / bonus adjusted, like the rest of the prices), keeps it in its own file (data/history.db) and refreshes it when a
newer session exists. Nothing else reads this file, so it cannot disturb the nightly jobs.
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from . import market

HIST_DB = Path(__file__).resolve().parent.parent / "data" / "history.db"
IST = ZoneInfo("Asia/Kolkata")
SCHEMA = """
CREATE TABLE IF NOT EXISTS hist(sym TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v INTEGER, PRIMARY KEY(sym, d));
CREATE TABLE IF NOT EXISTS fetched(sym TEXT PRIMARY KEY, at REAL, last TEXT, n INTEGER);
"""


def connect():
    HIST_DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(HIST_DB, timeout=60)
    con.executescript(SCHEMA)
    return con


def latest_session():
    row = market.connect().execute("SELECT MAX(d) FROM idx").fetchone()
    return row[0] if row and row[0] else None


INDEXES = {"NIFTY500": market.INDEX}          # symbols that are not NSE stocks: the chart's relative-strength line uses the Nifty 500


def fetch(sym):
    """Every daily bar Yahoo has for the stock, oldest first, as [(d, o, h, l, c, v)]."""
    df = yf.Ticker(INDEXES.get(sym) or sym + ".NS").history(period="max", interval="1d", auto_adjust=True).dropna(subset=["Close"])
    today = datetime.now(IST)
    out = []
    for i, r in df.iterrows():
        o, h, l, c, v = (float(r.Open), float(r.High), float(r.Low), float(r.Close), int(r.Volume or 0))
        if min(o, h, l, c) <= 0 or h < l:
            continue
        if v == 0 and o == h == l == c and sym not in INDEXES:                       # a flat copy of the last close on a market holiday
            continue
        d = i.strftime("%Y-%m-%d")
        if d == today.strftime("%Y-%m-%d") and today.hour * 60 + today.minute < 15 * 60 + 45:
            continue                                         # today's candle is unfinished while the market is open
        out.append((d, round(o, 2), round(h, 2), round(l, 2), round(c, 2), v))
    return out


def get(sym):
    """The stock's stored full history, fetched or refreshed first when it is missing or behind the market."""
    con = connect()
    row = con.execute("SELECT at, last, n FROM fetched WHERE sym=?", (sym,)).fetchone()
    session = latest_session()
    stale = row is None or (session and row[1] < session and time.time() - row[0] > 3600)
    if stale:
        try:
            bars = fetch(sym)
        except Exception:
            bars = []
        if bars and (row is None or len(bars) >= 0.9 * row[2]):     # never replace a good history with a thin one
            with con:
                con.execute("DELETE FROM hist WHERE sym=?", (sym,))
                con.executemany("INSERT INTO hist VALUES(?,?,?,?,?,?,?)", [(sym, *b) for b in bars])
                con.execute("INSERT OR REPLACE INTO fetched VALUES(?,?,?,?)", (sym, time.time(), bars[-1][0], len(bars)))
        elif row is not None:
            with con:
                con.execute("UPDATE fetched SET at=? WHERE sym=?", (time.time(), sym))     # do not retry for an hour
    return [list(r) for r in con.execute("SELECT d,o,h,l,c,v FROM hist WHERE sym=? ORDER BY d", (sym,))]
