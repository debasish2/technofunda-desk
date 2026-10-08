"""Industry classification and market capitalisation for every NSE stock.

    python -m backend.classify             # classify stocks not done yet and refresh market caps (resumable)
    python -m backend.classify --all       # redo every stock's classification as well

NSE and BSE share one four-tier industry structure (since 2022): macro-economic sector, sector, industry, basic industry. BSE's
public quote header carries all four for every listed security (its Sector / IndustryNew / IGroup / ISubGroup fields line up with
NSE's macro / sector / industry / basic industry, e.g. RELIANCE = Energy / Oil, Gas & Consumable Fuels / Petroleum Products /
Refineries & Marketing). One BSE list call maps ISINs to BSE codes and gives market caps; then one small call per stock fetches the
four names. Stored in market.db, table class.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import market

HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com", "Accept": "application/json"}
LIST = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
HEADER = "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w"
SCHEMA = """CREATE TABLE IF NOT EXISTS class (sym TEXT PRIMARY KEY, isin TEXT, scrip TEXT, macro TEXT, sector TEXT, industry TEXT,
            basic TEXT, mcap REAL, updated TEXT)"""


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def tidy(s):
    return " ".join((s or "").split())              # BSE writes "Telecom -  Equipment & Accessories" with a double space


def bse_list():
    r = requests.get(LIST, params={"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"}, headers=HEAD, timeout=90)
    r.raise_for_status()
    return {x["ISIN_NUMBER"]: (x["SCRIP_CD"], float(x["Mktcap"]) if x.get("Mktcap") not in (None, "", "-") else None) for x in r.json() if x.get("ISIN_NUMBER")}


def header(scrip):
    for i in range(3):
        try:
            r = requests.get(HEADER, params={"quotetype": "EQ", "scripcode": scrip}, headers=HEAD, timeout=30)
            if r.ok:
                j = r.json()
                return tidy(j.get("Sector")), tidy(j.get("IndustryNew")), tidy(j.get("IGroup")), tidy(j.get("ISubGroup"))
        except Exception:
            pass
        time.sleep(1 + i)
    return None


def run(redo=False, log=log):
    con = market.connect()
    con.execute(SCHEMA)
    uni = con.execute("SELECT sym, isin FROM universe WHERE isin IS NOT NULL AND isin != ''").fetchall()
    lst = bse_list()
    have = {r[0] for r in con.execute("SELECT sym FROM class WHERE basic IS NOT NULL")}
    todo, nobse = [], 0
    for sym, isin in uni:
        if isin not in lst:
            nobse += 1
            continue
        if redo or sym not in have:
            todo.append((sym, isin, lst[isin][0]))
    log(f"{len(uni)} NSE stocks, {nobse} not listed on BSE, {len(todo)} to classify")
    done = failed = 0
    now = time.strftime("%Y-%m-%d")
    with ThreadPoolExecutor(5) as ex:
        for (sym, isin, scrip), h in zip(todo, ex.map(lambda t: header(t[2]), todo)):
            if h is None or not h[3]:
                failed += 1
                continue
            con.execute("INSERT OR REPLACE INTO class(sym,isin,scrip,macro,sector,industry,basic,mcap,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (sym, isin, scrip, h[0], h[1], h[2], h[3], lst[isin][1], now))
            done += 1
            if done % 200 == 0:
                con.commit()
                log(f"  {done} done")
    con.commit()
    # market caps move every day: refresh them for everything already classified
    n = 0
    for sym, isin in uni:
        if isin in lst and lst[isin][1] is not None:
            con.execute("UPDATE class SET mcap=? WHERE sym=?", (lst[isin][1], sym))
            n += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM class WHERE basic IS NOT NULL").fetchone()[0]
    log(f"classified {done} now ({failed} failed); {total} stocks have a classification; market caps refreshed for {n}")
    return total


if __name__ == "__main__":
    run(redo="--all" in sys.argv)
