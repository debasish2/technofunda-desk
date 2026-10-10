"""Mutual funds from AMFI, the industry body: every scheme, its daily NAV history, and the returns and risk figures worked out from it. All free.

    python -m backend.funds --schemes          # the scheme list (about 14,000 NAV lines) with category, fund house, plan and option
    python -m backend.funds --history 10       # NAV history for the last 10 years of every kept scheme (about 85 s a year of data, in the background)
    python -m backend.funds --daily            # today's NAVs (the nightly job runs this) and the missing days since the last one
    python -m backend.funds --metrics          # returns, volatility, drawdown, Sharpe for every kept scheme
    python -m backend.funds --report           # what is stored

Sources: https://www.amfiindia.com/spages/NAVAll.txt (latest NAV of every scheme, grouped by category and fund house) and
https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx (all schemes' NAVs for a date range; one year is about 300 MB of text).
Only growth-option schemes of open-ended funds are kept: the dividend (IDCW) lines of the same fund repeat the NAV history with payouts
taken out, which would double the table without adding a fund. Closed-ended and interval schemes are listed but get no history.
"""
import math
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

DB = Path(__file__).resolve().parent.parent / "data" / "funds.db"
NAVALL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={frm}&todt={to}"
UA = {"User-Agent": "Mozilla/5.0"}
RF = 0.065                                   # risk-free rate used for Sharpe: about the 10-year government bond yield
SCHEMA = """
CREATE TABLE IF NOT EXISTS scheme (code TEXT PRIMARY KEY, isin TEXT, name TEXT, amc TEXT, kind TEXT, category TEXT, plan TEXT, opt TEXT, nav REAL, nav_date TEXT, keep INTEGER);
CREATE TABLE IF NOT EXISTS nav (code TEXT, d TEXT, v REAL, PRIMARY KEY (code, d)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS metric (code TEXT PRIMARY KEY, r1w REAL, r1m REAL, r3m REAL, r6m REAL, r1y REAL, r3y REAL, r5y REAL, r10y REAL,
                                   vol3y REAL, dd3y REAL, sharpe3y REAL, hi52 REAL, lo52 REAL, first_d TEXT, n INTEGER, updated TEXT);
CREATE INDEX IF NOT EXISTS scheme_cat ON scheme(category);
"""


def connect():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _d(s):
    return datetime.strptime(s.strip(), "%d-%b-%Y").date().isoformat()


def parse_navall(text):
    """Rows from NAVAll.txt: a category line, then fund-house lines, then scheme lines (code;isin;isin2;name;plan;option;nav;date)."""
    out, kind, cat, amc = [], None, None, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Scheme Code"):
            continue
        m = re.match(r"^(Open Ended Schemes|Close Ended Schemes|Interval Fund Schemes)\s*\((.*)\)\s*$", line)
        if m:
            kind, cat = m.group(1).replace(" Schemes", ""), re.sub(r"\s+", " ", m.group(2).replace("\u0080\u0099", "'")).strip()
            continue
        if ";" not in line:
            amc = line
            continue
        p = [x.strip() for x in line.split(";")]
        if len(p) < 8 or not p[0].isdigit():
            continue
        try:
            nav = float(p[6])
        except ValueError:
            continue
        try:
            nd = _d(p[7])
        except ValueError:
            continue
        isin = p[1] if p[1] not in ("", "-") else (p[2] if p[2] not in ("", "-") else None)
        out.append((p[0], isin, p[3], amc, kind, cat, p[4] or None, p[5] or None, nav, nd))
    return out


def refresh_schemes(con=None, log=print):
    con = con or connect()
    r = requests.get(NAVALL, headers=UA, timeout=90)
    r.raise_for_status()
    rows = parse_navall(r.content.decode("utf-8", errors="replace"))
    if len(rows) < 5000:
        raise RuntimeError(f"AMFI's list has only {len(rows)} schemes; keeping the stored one")
    keep = lambda kind, opt, name: int(kind == "Open Ended" and bool(opt) and "growth" in opt.lower() and "idcw" not in opt.lower() and "dividend" not in opt.lower())
    con.executemany("""INSERT INTO scheme(code,isin,name,amc,kind,category,plan,opt,nav,nav_date,keep) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET isin=excluded.isin,name=excluded.name,amc=excluded.amc,kind=excluded.kind,category=excluded.category,
                       plan=excluded.plan,opt=excluded.opt,nav=excluded.nav,nav_date=excluded.nav_date,keep=excluded.keep""",
                    [(c, i, n, a, k, cat, pl, o, nv, nd, keep(k, o, n)) for c, i, n, a, k, cat, pl, o, nv, nd in rows])
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM scheme WHERE keep=1").fetchone()[0]
    log(f"schemes: {len(rows)} listed, {n} kept (open-ended growth options)")
    return len(rows), n


def _load_range(con, frm, to, keep, log=print):
    """Stream one date range of AMFI's history file into the nav table, keeping only `keep` scheme codes. Returns rows stored."""
    r = requests.get(HISTORY.format(frm=frm.strftime("%d-%b-%Y"), to=to.strftime("%d-%b-%Y")), headers=UA, timeout=600, stream=True)
    r.raise_for_status()
    batch, n, seen = [], 0, False
    for raw in r.iter_lines(decode_unicode=False):
        if not seen and raw.strip():
            seen = True
            if raw.lstrip()[:1] == b"<":                       # AMFI answers a range it dislikes with a web page, not data: never read that as "no NAVs"
                raise RuntimeError("AMFI returned a web page instead of NAV data for " + str(frm) + " to " + str(to))
        if not raw or not raw[:1].isdigit():
            continue
        p = raw.decode("utf-8", errors="replace").split(";")
        if len(p) < 8 or p[0] not in keep:
            continue
        try:
            batch.append((p[0], _d(p[7]), float(p[6])))
        except ValueError:
            continue
        if len(batch) >= 20000:
            con.executemany("INSERT OR REPLACE INTO nav VALUES(?,?,?)", batch)
            n += len(batch)
            batch = []
    if batch:
        con.executemany("INSERT OR REPLACE INTO nav VALUES(?,?,?)", batch)
        n += len(batch)
    con.commit()
    return n


def load_history(years=10, con=None, log=print):
    """NAV history for the last `years` years, a year of AMFI's file at a time. Years already stored are skipped, so a stopped run carries on."""
    con = con or connect()
    keep = {r[0] for r in con.execute("SELECT code FROM scheme WHERE keep=1")}
    if not keep:
        refresh_schemes(con, log)
        keep = {r[0] for r in con.execute("SELECT code FROM scheme WHERE keep=1")}
    today = date.today()
    chunk = 89                                                  # AMFI serves ranges up to about three months reliably; a year at a time it often refuses
    for i in range(math.ceil(years * 365 / (chunk + 1))):
        hi = today - timedelta(days=(chunk + 1) * i)
        lo = hi - timedelta(days=chunk)
        have = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT d FROM nav WHERE d BETWEEN ? AND ?)", (lo.isoformat(), hi.isoformat())).fetchone()[0]
        if have >= 50:
            log(f"  {lo} to {hi}: already stored ({have} days)", flush=True) if False else log(f"  {lo} to {hi}: already stored ({have} days)")
            continue
        t = time.time()
        for attempt in range(4):
            try:
                n = _load_range(con, lo, hi, keep, log)
                break
            except Exception as e:
                log(f"  {lo} to {hi}: attempt {attempt + 1} failed ({type(e).__name__}: {str(e)[:80]})")
                time.sleep(20 * (attempt + 1))
        else:                                                   # AMFI sometimes refuses one particular three-month range: take it in 30-day pieces
            n, bad = 0, 0
            for k in range(3):
                a0 = lo + timedelta(days=30 * k)
                b0 = min(hi, a0 + timedelta(days=29))
                try:
                    n += _load_range(con, a0, b0, keep, log)
                except Exception as e:
                    bad += 1
                    log(f"    {a0} to {b0}: failed ({type(e).__name__})")
            if bad:
                log(f"  {lo} to {hi}: {bad} piece(s) failed; run the same command again to retry")
                continue
        log(f"  {lo} to {hi}: {n:,} NAVs stored ({time.time() - t:.0f}s)")


def daily(con=None, log=print):
    """Today's NAVs from NAVAll (all kept schemes), plus any missing days since the newest stored one from the history file."""
    con = con or connect()
    refresh_schemes(con, log)
    last = con.execute("SELECT MAX(d) FROM nav").fetchone()[0]
    if last:
        frm = date.fromisoformat(last) + timedelta(days=1)
        if (date.today() - frm).days >= 0 and (date.today() - frm).days < 360:
            keep = {r[0] for r in con.execute("SELECT code FROM scheme WHERE keep=1")}
            try:
                n = _load_range(con, frm, date.today(), keep, log)
                log(f"  NAVs since {frm}: {n:,}")
            except Exception as e:
                log(f"  history catch-up failed ({type(e).__name__}); today's list is stored below")
    con.execute("INSERT OR REPLACE INTO nav SELECT code, nav_date, nav FROM scheme WHERE keep=1 AND nav IS NOT NULL")
    con.commit()
    return metrics(con, log)


def _pct(a, b):
    return (a / b - 1) * 100 if a and b and b > 0 else None


def series_metrics(dates, vals):
    """Returns, volatility, drawdown and Sharpe from one scheme's NAV series (dates ascending, ISO strings)."""
    import numpy as np
    import pandas as pd
    s = pd.Series(vals, index=pd.to_datetime(dates)).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s = s[s > 0]                                                  # a zero NAV is a placeholder (unclaimed-dividend pools and the like), never a price
    if len(s) < 5:
        return None
    last_d, last = s.index[-1], float(s.iloc[-1])

    def at(days):
        t = last_d - pd.Timedelta(days=days)
        x = s[:t]
        return float(x.iloc[-1]) if len(x) and (t - x.index[-1]).days < 10 else None
    cagr = lambda days, yrs: ((last / at(days)) ** (1 / yrs) - 1) * 100 if at(days) and at(days) > 0 else None
    m = {"r1w": _pct(last, at(7)), "r1m": _pct(last, at(30)), "r3m": _pct(last, at(91)), "r6m": _pct(last, at(182)), "r1y": _pct(last, at(365)),
         "r3y": cagr(1095, 3), "r5y": cagr(1826, 5), "r10y": cagr(3652, 10)}
    w = s[s.index > last_d - pd.Timedelta(days=1095)]
    if len(w) > 200:
        ret = np.log(w).diff().dropna()
        vol = float(ret.std() * math.sqrt(250))
        m["vol3y"] = vol * 100
        m["dd3y"] = float(((w / w.cummax()) - 1).min() * 100)
        yrs = (w.index[-1] - w.index[0]).days / 365.25
        ann = (float(w.iloc[-1]) / float(w.iloc[0])) ** (1 / yrs) - 1 if yrs > 0.5 else None
        m["sharpe3y"] = (ann - RF) / vol if ann is not None and vol > 0 else None
    y = s[s.index > last_d - pd.Timedelta(days=365)]
    m["hi52"], m["lo52"] = (float(y.max()), float(y.min())) if len(y) else (None, None)
    m["first_d"], m["n"], m["last_d"] = s.index[0].date().isoformat(), int(len(s)), last_d.date().isoformat()
    return m


def metrics(con=None, log=print):
    con = con or connect()
    import pandas as pd
    df = pd.read_sql("SELECT code, d, v FROM nav ORDER BY code, d", con)
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    newest = date.fromisoformat(df["d"].max()) if len(df) else None
    stale = 0
    for code, g in df.groupby("code"):
        try:
            m = series_metrics(g["d"].tolist(), g["v"].tolist())
        except Exception as e:                                    # one odd scheme must not stop the other 3,600
            log(f"  {code}: skipped ({type(e).__name__}: {e})")
            continue
        if m and newest and (newest - date.fromisoformat(m["last_d"])).days > 7:
            stale += 1                                            # a scheme that stopped publishing NAVs (merged or wound up): its old returns would mislead
            continue
        if m:
            rows.append((code, *(m.get(k) for k in ("r1w", "r1m", "r3m", "r6m", "r1y", "r3y", "r5y", "r10y", "vol3y", "dd3y", "sharpe3y", "hi52", "lo52", "first_d", "n")), now))
    con.execute("DELETE FROM metric")
    con.executemany("INSERT INTO metric VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    log(f"metrics: {len(rows)} schemes ({stale} left out: no NAV in the last week)")
    return len(rows)


def report(con=None):
    con = con or connect()
    q = lambda sql: con.execute(sql).fetchone()[0]
    print(f"schemes listed {q('SELECT COUNT(*) FROM scheme')}, kept {q('SELECT COUNT(*) FROM scheme WHERE keep=1')}; NAV rows {q('SELECT COUNT(*) FROM nav'):,}"
          f" for {q('SELECT COUNT(DISTINCT code) FROM nav')} schemes, {q('SELECT MIN(d) FROM nav')} to {q('SELECT MAX(d) FROM nav')}; metrics for {q('SELECT COUNT(*) FROM metric')}")
    for r in con.execute("SELECT category, COUNT(*) n FROM scheme WHERE keep=1 GROUP BY category ORDER BY n DESC LIMIT 12"):
        print(f"  {r['n']:>5}  {r['category']}")


if __name__ == "__main__":
    a = sys.argv[1:]
    c = connect()
    if "--schemes" in a:
        refresh_schemes(c)
    elif "--history" in a:
        load_history(int(a[a.index("--history") + 1]), c)
        metrics(c)
    elif "--daily" in a:
        daily(c)
    elif "--metrics" in a:
        metrics(c)
    else:
        report(c)
