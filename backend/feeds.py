"""Free feeds for the Desk and the Events page: analyst views, news, corporate actions, IPOs and the board-meeting calendar.

    python -m backend.feeds analysts TCS      # print what the endpoint returns (also: news, actions)
    python -m backend.feeds ipos
    python -m backend.feeds calendar

Sources, all free:
  analysts   Yahoo Finance: price targets, buy/hold/sell counts by month, EPS and revenue estimates, growth against the index
  news       Google News search for the company (headlines with source and date; the link opens the article)
  actions    NSE's corporate-actions list (ex-dates and record dates, upcoming and recent) plus Yahoo's dividend and split history
  ipos       NSE's IPO lists: open and upcoming issues, and about 1,500 past issues with dates, price band and listing date
  calendar   NSE's board-meeting calendar: when each company meets to approve results or declare a dividend
Answers are kept in data/feeds.db for a while (a few hours for NSE and Yahoo, half an hour for news), so opening a page twice does not ask twice.
"""
import json
import re
import sqlite3
import sys
import time
import warnings
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

warnings.filterwarnings("ignore")
DB = Path(__file__).resolve().parent.parent / "data" / "feeds.db"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36"}
TTL = {"analysts": 12 * 3600, "news": 30 * 60, "actions": 6 * 3600, "ipos": 3600, "calendar": 3 * 3600}
_nse = {"s": None, "t": 0}


def _db():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=60)
    con.execute("CREATE TABLE IF NOT EXISTS feed_cache (kind TEXT, key TEXT, fetched REAL, body TEXT, PRIMARY KEY (kind, key))")
    return con


def cached(kind, key, build, refresh=False):
    """Answer from the cache when it is fresh enough; otherwise build it (and keep the old answer if building fails)."""
    con = _db()
    r = con.execute("SELECT fetched, body FROM feed_cache WHERE kind=? AND key=?", (kind, key)).fetchone()
    if r and not refresh and time.time() - r[0] < TTL[kind]:
        return json.loads(r[1])
    try:
        out = build()
    except Exception as e:
        if r:
            return json.loads(r[1])
        return {"ok": False, "why": f"The data source did not answer ({type(e).__name__}). Try again in a moment."}
    if out.get("ok", True):
        con.execute("INSERT OR REPLACE INTO feed_cache VALUES(?,?,?,?)", (kind, key, time.time(), json.dumps(out)))
        con.commit()
    return out


def nse_get(path):
    """One NSE API call. NSE wants a cookie from its home page first; the session is reused for ten minutes."""
    now = time.time()
    if _nse["s"] is None or now - _nse["t"] > 600:
        s = requests.Session()
        s.headers.update(UA | {"Accept": "application/json", "Referer": "https://www.nseindia.com/"})
        s.get("https://www.nseindia.com/", timeout=25)
        _nse["s"], _nse["t"] = s, now
    for attempt in range(2):
        r = _nse["s"].get("https://www.nseindia.com/api/" + path, timeout=30)
        if r.status_code in (401, 403) and attempt == 0:
            _nse["s"] = None
            return nse_get(path)
        r.raise_for_status()
        return r.json()


def company_name(sym):
    """A search-friendly company name: the screener's, else the NSE list's, without the legal suffix."""
    name = None
    try:
        from . import db
        r = db.connect().execute("SELECT name FROM stock WHERE sym=?", (sym,)).fetchone()
        name = r["name"] if r and r["name"] else None
    except Exception:
        pass
    if not name:
        try:
            from . import market
            r = market.connect().execute("SELECT name FROM universe WHERE sym=?", (sym,)).fetchone()
            name = r[0] if r and r[0] else None
        except Exception:
            pass
    name = name or sym
    return re.sub(r"\s+(Limited|Ltd\.?)$", "", name, flags=re.I).strip()


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- analysts (Yahoo)
PERIODS = {"0q": "Current quarter", "+1q": "Next quarter", "0y": "Current year", "+1y": "Next year", "LTG": "Long-term growth"}


def analysts(sym, refresh=False):
    def build():
        import yfinance as yf
        t = yf.Ticker(sym + ".NS")

        def frame(df, scale=1.0, cols=None):
            if df is None or getattr(df, "empty", True):
                return []
            out = []
            for idx, row in df.iterrows():
                d = {"period": PERIODS.get(str(idx), str(idx))}
                for c in (cols or df.columns):
                    v = _num(row.get(c)) if c in df.columns else None
                    d[c] = None if v is None else v * scale if c in ("avg", "low", "high", "yearAgoEps", "yearAgoRevenue") else v
                out.append(d)
            return out
        try:
            tg = t.analyst_price_targets or {}
        except Exception:
            tg = {}
        targets = {k: _num(tg.get(k)) for k in ("current", "low", "mean", "median", "high")}
        try:
            rc = t.recommendations_summary
            recs = [] if rc is None or rc.empty else [{"period": r["period"], "strongBuy": int(r["strongBuy"]), "buy": int(r["buy"]), "hold": int(r["hold"]), "sell": int(r["sell"]), "strongSell": int(r["strongSell"])} for _, r in rc.iterrows()]
        except Exception:
            recs = []
        try:
            eps = frame(t.earnings_estimate, cols=["avg", "low", "high", "yearAgoEps", "numberOfAnalysts", "growth"])
        except Exception:
            eps = []
        try:
            rev = frame(t.revenue_estimate, scale=1e-7, cols=["avg", "low", "high", "yearAgoRevenue", "numberOfAnalysts", "growth"])      # rupees to crore
        except Exception:
            rev = []
        try:
            gr = t.growth_estimates
            growth = [] if gr is None or gr.empty else [{"period": PERIODS.get(str(i), str(i)), "stock": _num(r.get("stockTrend")), "index": _num(r.get("indexTrend"))} for i, r in gr.iterrows()]
        except Exception:
            growth = []
        if not (targets.get("mean") or recs or eps):
            return {"ok": False, "why": "Yahoo Finance has no analyst coverage for this stock."}
        return {"ok": True, "targets": targets, "recs": recs, "eps": eps, "revenue": rev, "growth": growth, "asof": datetime.now().isoformat(timespec="minutes")}
    return cached("analysts", sym, build, refresh)


# ---------------------------------------------------------------- news (Google News)
def news(sym, refresh=False):
    def build():
        name = company_name(sym)
        q = quote(f'"{name}" (share OR stock OR shares OR results) when:90d')
        r = requests.get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en", headers=UA, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out, seen = [], set()
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            src_el = it.find("source")
            src = (src_el.text or "").strip() if src_el is not None else ""
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            key = re.sub(r"\W+", "", title.lower())[:80]
            if not title or key in seen:
                continue
            seen.add(key)
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone().isoformat(timespec="minutes")
            except Exception:
                dt = None
            out.append({"title": title, "url": (it.findtext("link") or "").strip(), "source": src, "dt": dt})
        out.sort(key=lambda x: x["dt"] or "", reverse=True)
        return {"ok": True, "query": name, "items": out[:40], "asof": datetime.now().isoformat(timespec="minutes")}
    return cached("news", sym, build, refresh)


# ---------------------------------------------------------------- corporate actions (NSE + Yahoo)
def actions(sym, refresh=False):
    def build():
        nse_rows, events = [], []
        try:
            for x in nse_get(f"corporates-corporateActions?index=equities&symbol={quote(sym)}"):
                nse_rows.append({"subject": x.get("subject"), "ex": x.get("exDate"), "record": x.get("recDate"), "series": x.get("series"), "face": x.get("faceVal")})
        except Exception:
            pass
        try:
            events = [e for e in calendar().get("rows", []) if e["symbol"] == sym]
        except Exception:
            pass
        divs, splits = [], []
        try:
            import yfinance as yf
            t = yf.Ticker(sym + ".NS")
            divs = [{"d": i.date().isoformat(), "amount": float(v)} for i, v in t.dividends.items()][-25:][::-1]
            splits = [{"d": i.date().isoformat(), "ratio": float(v)} for i, v in t.splits.items()][::-1]
        except Exception:
            pass
        if not (nse_rows or divs or splits or events):
            return {"ok": False, "why": "No corporate actions on record for this stock."}
        return {"ok": True, "nse": nse_rows, "events": events, "dividends": divs, "splits": splits, "asof": datetime.now().isoformat(timespec="minutes")}
    return cached("actions", sym, build, refresh)


# ---------------------------------------------------------------- IPOs (NSE)
def _d(s):
    for f in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime((s or "").strip().title(), f).date()
        except ValueError:
            pass
    return None


def ipos(refresh=False):
    def build():
        today = date.today()
        rows = []
        for x in nse_get("public-past-issues"):
            a, b, l = _d(x.get("ipoStartDate")), _d(x.get("ipoEndDate")), _d(x.get("listingDate"))
            if not a:
                continue
            status = "Upcoming" if a > today else "Open" if b and b >= today else "Listed" if l and l <= today else "Closed"
            rows.append({"company": x.get("companyName"), "symbol": x.get("symbol"), "type": x.get("securityType"), "open": a.isoformat(), "close": b.isoformat() if b else None,
                         "band": x.get("priceRange") if x.get("priceRange") not in (None, "-") else x.get("issuePrice"), "listing": l.isoformat() if l else None, "status": status})
        for path in ("ipo-current-issue", "all-upcoming-issues?category=ipo"):
            try:
                data = nse_get(path)
            except Exception:
                data = []
            for x in data if isinstance(data, list) else []:
                a, b = _d(x.get("issueStartDate")), _d(x.get("issueEndDate"))
                if not a or any(r["symbol"] == x.get("symbol") for r in rows):
                    continue
                rows.append({"company": x.get("companyName") or x.get("symbol"), "symbol": x.get("symbol"), "type": x.get("series"), "open": a.isoformat(), "close": b.isoformat() if b else None,
                             "band": x.get("issuePrice"), "listing": None, "status": x.get("status") or ("Upcoming" if a > today else "Open")})
        rows.sort(key=lambda r: r["open"], reverse=True)
        cutoff = (today - timedelta(days=500)).isoformat()
        return {"ok": True, "rows": [r for r in rows if r["open"] >= cutoff][:400], "asof": datetime.now().isoformat(timespec="minutes"), "total": len(rows)}
    return cached("ipos", "all", build, refresh)


# ---------------------------------------------------------------- board-meeting calendar (NSE)
def calendar(refresh=False):
    def build():
        rows = []
        for x in nse_get("event-calendar"):
            d = _d(x.get("date"))
            if not d:
                continue
            rows.append({"symbol": x.get("symbol"), "company": x.get("company"), "purpose": x.get("purpose"), "detail": x.get("bm_desc"), "date": d.isoformat()})
        rows.sort(key=lambda r: (r["date"], r["symbol"] or ""))
        return {"ok": True, "rows": rows, "asof": datetime.now().isoformat(timespec="minutes")}
    return cached("calendar", "all", build, refresh)


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "analysts"
    arg = sys.argv[2].upper() if len(sys.argv) > 2 else "TCS"
    out = {"analysts": lambda: analysts(arg), "news": lambda: news(arg), "actions": lambda: actions(arg), "ipos": ipos, "calendar": calendar}[kind]()
    print(json.dumps(out, indent=1)[:2500])
