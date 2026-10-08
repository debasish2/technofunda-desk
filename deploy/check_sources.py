#!/usr/bin/env python3
"""Can THIS machine reach the free data sources TechnoFunda Desk lives on?  Run it on the new server BEFORE moving anything.

    python3 check_sources.py            # standard library only: nothing to install
    python3 check_sources.py --yf       # also tries yfinance (use it after setup_server.sh, with the app's own Python)

NSE, BSE and Yahoo sometimes refuse or throttle requests that come from data-centre addresses. If a line below says FAIL, the server
location or provider is a poor fit: delete the server and try another provider or city before spending more time on it.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.cookiejar import CookieJar

UA = "Mozilla/5.0"          # the same plain agent string the app itself sends; a longer browser-like one is refused by NSE and BSE
jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
results = []


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    t = time.time()
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, b"", time.time() - t
    except Exception as e:                                       # DNS, timeout, reset, TLS...
        return type(e).__name__, str(e).encode(), time.time() - t


def check(name, url, ok=lambda body: True, headers=None):
    status, body, secs = get(url, headers)
    good = status == 200 and ok(body)
    results.append(good)
    note = "" if good else f"  <- {status} {body[:80]!r}"
    print(f"{'PASS' if good else 'FAIL'}  {name:<46} {secs:5.1f}s  {len(body):>9,} bytes{note}")
    return good


def last_weekday():
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def main():
    print("Where this machine appears to be:")
    s, b, _ = get("https://ipinfo.io/json", timeout=15)
    if s == 200:
        j = json.loads(b)
        print(f"  {j.get('ip')}  {j.get('city')}, {j.get('region')}, {j.get('country')}  ({j.get('org')})")
    else:
        print("  (could not look it up)")
    print()
    # NSE: the home page first (it hands out the cookies its API wants), then the files and APIs the app uses
    check("NSE home page", "https://www.nseindia.com/", headers={"Accept": "text/html"})
    d = last_weekday()
    for _ in range(4):                                            # the newest weekday may be a holiday
        if check(f"NSE end-of-day file {d:%d-%b}", f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
                 ok=lambda body: b"SYMBOL" in body[:60].upper()):
            break
        d -= timedelta(days=1)
    nse_h = {"Accept": "application/json", "Referer": "https://www.nseindia.com/"}
    check("NSE equity list", "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", ok=lambda body: b"SYMBOL" in body[:80].upper())
    check("NSE annual results API (TITAN)", "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=TITAN&period=Annual",
          ok=lambda body: body[:1] == b"[", headers=nse_h)
    check("NSE announcements API (TITAN)", "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=TITAN",
          ok=lambda body: body[:1] in (b"[", b"{"), headers=nse_h)
    # BSE
    bse_h = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com", "Accept": "application/json"}
    check("BSE company header (TITAN)", "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w?quotetype=EQ&scripcode=500114",
          ok=lambda body: body[:1] == b"{", headers=bse_h)
    check("BSE announcements (TITAN)", f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=Result&strPrevDate={(date.today() - timedelta(days=120)):%Y%m%d}"
          f"&strScrip=500114&strSearch=P&strToDate={date.today():%Y%m%d}&strType=C", ok=lambda body: body[:1] == b"{", headers=bse_h)
    # Yahoo
    check("Yahoo price chart (TCS)", "https://query1.finance.yahoo.com/v8/finance/chart/TCS.NS?range=5d&interval=1d", ok=lambda body: b'"chart"' in body[:30])
    print()
    if "--yf" in sys.argv:
        try:
            import yfinance as yf
            t = time.time()
            h = yf.Ticker("TCS.NS").history(period="5d")
            info = yf.Ticker("TITAN.NS").info
            good = len(h) > 0 and bool(info.get("longBusinessSummary"))
            results.append(good)
            print(f"{'PASS' if good else 'FAIL'}  yfinance prices + company info                {time.time() - t:5.1f}s  ({len(h)} bars, summary {'yes' if info.get('longBusinessSummary') else 'no'})")
        except Exception as e:
            results.append(False)
            print(f"FAIL  yfinance: {type(e).__name__}: {e}")
        print()
    bad = results.count(False)
    if bad == 0:
        print("All reachable. This server can run the app.")
    else:
        print(f"{bad} of {len(results)} checks failed. Read the lines marked FAIL: a blocked NSE/BSE address is usually permanent for that provider.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
