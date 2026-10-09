"""A company's announcements from BSE's own feed: concall transcripts, recordings and presentations, credit ratings and everything else it files.

    python -m backend.announcements TCS       # fetch and print what the Documents tab will show

One free call chain per stock (50 announcements a page, about a year back), kept in the database for a few hours so opening a stock twice does not
ask BSE twice. The same feed already carries the results filings the site reads; here it is read in full and sorted by kind:
  transcript   Earnings Call Transcript            audio     recording of the earnings call
  presentation Investor Presentation               meeting   analyst / investor meet notice (date, dial-in, schedule)
  rating       Credit Rating (ESG ratings are not credit ratings)
  result, board, corp_action, agm, press, other
"""
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests

from . import db
from .sources.bse import HEAD

URL = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={page}&strCat=-1"
       "&strPrevDate={frm}&strScrip={scrip}&strSearch=P&strToDate={to}&strType=C&subcategory=-1")
ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"
DAYS = 360                    # BSE refuses a range of more than 12 months
TTL = 3 * 3600
RECENT = 80


def kind(row):
    cat = (row.get("CATEGORYNAME") or "").strip()
    sub = (row.get("SUBCATNAME") or "").strip()
    text = ((row.get("NEWSSUB") or "") + " " + (row.get("HEADLINE") or "")).lower()
    if sub == "Earnings Call Transcript" or ("transcript" in text and ("call" in text or "meet" in text)):
        return "transcript"
    if row.get("AUDIO_VIDEO_FILE") or ("audio" in text and ("recording" in text or "call" in text)):
        return "audio"
    if sub == "Investor Presentation" or "investor presentation" in text or "earnings presentation" in text:
        return "presentation"
    if sub == "Analyst / Investor Meet":
        return "meeting"
    if sub == "Credit Rating" or ("credit rating" in text and "esg" not in text):
        return "rating"
    if cat == "Result" or sub == "Financial Results":
        return "result"
    if cat == "Board Meeting":
        return "board"
    if "corp" in cat.lower() and "action" in cat.lower():
        return "corp_action"
    if cat == "AGM/EGM":
        return "agm"
    if sub in ("Press Release / Media Release", "Newspaper Publication"):
        return "press"
    return "other"


def _title(row):
    t = re.sub(r"\s+", " ", (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip())
    t = re.sub(r"^Announcement under Regulation 30 \(LODR\)\s*-\s*", "", t, flags=re.I)
    return t[:160] or "Announcement"


def fetch(scrip, con=None, days=DAYS):
    """Download the announcements of a BSE scrip for the last `days` days into the database; returns how many."""
    con = con or db.connect()
    s = requests.Session()
    to = date.today()
    frm = to - timedelta(days=days)
    rows, page = [], 1
    while page <= 25:
        r = s.get(URL.format(page=page, frm=frm.strftime("%Y%m%d"), to=to.strftime("%Y%m%d"), scrip=scrip), headers=HEAD, timeout=30).json()
        if r.get("Status") is False:
            raise RuntimeError(r.get("Message") or "BSE refused the request")
        part = r.get("Table") or []
        rows += part
        if len(part) < 50:
            break
        page += 1
    out = []
    for x in rows:
        if not x.get("NEWSID") or not x.get("DT_TM"):
            continue
        att = x.get("ATTACHMENTNAME") or ""
        out.append((str(scrip), str(x["NEWSID"]), x["DT_TM"][:19], kind(x), (x.get("CATEGORYNAME") or "").strip(), (x.get("SUBCATNAME") or "").strip(),
                    _title(x), ATTACH.format(name=att) if att else ""))
    con.executemany("INSERT OR REPLACE INTO announcement(scrip,newsid,dt,kind,cat,subcat,title,url) VALUES(?,?,?,?,?,?,?,?)", out)
    con.execute("INSERT OR REPLACE INTO ann_fetch VALUES(?,?)", (str(scrip), time.time()))
    con.commit()
    return len(out)


def get(sym, refresh=False):
    con = db.connect()
    r = con.execute("SELECT scrip FROM stock WHERE sym=?", (sym,)).fetchone()
    scrip = r["scrip"] if r and r["scrip"] else None
    if not scrip:
        return {"ok": False, "why": "This stock has no BSE code on file, so its announcements cannot be looked up."}
    f = con.execute("SELECT fetched FROM ann_fetch WHERE scrip=?", (str(scrip),)).fetchone()
    if refresh or not f or time.time() - f["fetched"] > TTL:
        try:
            fetch(scrip, con)
        except Exception as e:
            if not f:
                return {"ok": False, "why": f"BSE's announcements could not be loaded ({type(e).__name__}). Try again in a moment."}
    rows = [dict(x) for x in con.execute("SELECT dt,kind,cat,subcat,title,url FROM announcement WHERE scrip=? ORDER BY dt DESC", (str(scrip),))]
    cutoff = (date.today() - timedelta(days=DAYS)).isoformat()
    rows = [x for x in rows if x["dt"][:10] >= cutoff]
    pick = lambda *ks, n=60: [x for x in rows if x["kind"] in ks][:n]
    f = con.execute("SELECT fetched FROM ann_fetch WHERE scrip=?", (str(scrip),)).fetchone()
    return {"ok": True, "asof": datetime.fromtimestamp(f["fetched"]).isoformat(timespec="minutes") if f else None, "scrip": scrip,
            "concalls": pick("transcript", "audio", "presentation", "meeting"), "ratings": pick("rating"), "recent": rows[:RECENT],
            "counts": {k: sum(1 for x in rows if x["kind"] == k) for k in sorted({x["kind"] for x in rows})}}


if __name__ == "__main__":
    d = get(sys.argv[1].upper() if len(sys.argv) > 1 else "TCS")
    print({k: (len(v) if isinstance(v, list) else v) for k, v in d.items()})
    for key in ("concalls", "ratings"):
        for x in d.get(key, [])[:6]:
            print(f"  {key[:5]} {x['dt'][:10]} {x['kind']:<12} {x['title'][:90]}")
