"""Gap-filler from IndianAPI (stock.indianapi.in): quarterly results for stocks our free sources could not complete.

    python -m backend.indianapi --plan [--new] [--min-mcap 500]  # (--new: stocks never loaded) which stocks it would fetch, and the calls left this month
    python -m backend.indianapi --fetch 100 [--min-mcap 500] # fetch up to 100 stocks (one call each), check them, store the good ones
    python -m backend.indianapi --sym AEGISLOG,DEEPINDS      # fetch these stocks
    python -m backend.indianapi --fetch 20 --retry           # first re-ask the stocks that returned no data
    python -m backend.indianapi --fetch 2500 --all           # the audit pass: every company in the screener (one call each)
    python -m backend.indianapi --annual 2500                # yearly results of accepted stocks; fills missing annual profit
    python -m backend.indianapi --banks --max-calls 300      # banks, lenders and insurers: all statements, verified by net profit
    python -m backend.indianapi --stats all --max-calls 1200   # EVERY statement of accepted stocks in one call each, biggest first, resumable (use this)
    python -m backend.indianapi --stats balancesheet,cashflow --max-calls 1000   # single statements (costs one call per statement: avoid)
    python -m backend.indianapi --audit-report               # data/ia_audit.csv: quarters where IndianAPI and our row differ
    python -m backend.indianapi --apply                      # write the stored quarters into `quarter` (also done after every Yahoo stitch)
    python -m backend.indianapi --report                     # what was accepted and what was not, and why

The key is read from .env (INDIANAPI_KEY=...), which git ignores. The free plan allows 500 calls a month (INDIANAPI_MONTHLY_LIMIT).

What it is trusted for. IndianAPI's rows are rounded to whole rupee crore, and its "Net Profit" includes minority interest, so it
only ever fills quarters that are missing or whose own filing parse was flagged; it never replaces a filing or an unflagged row.
A stock is accepted only if its figures agree with what we already hold for the quarters both have (that also proves the name
search found the right company). Net profit is scaled by the stock's own attributable/total ratio when that ratio is steady.
Rows are stored in ia_quarter, so a nightly rebuild of `quarter` never loses them and no call is ever repeated.
Source is 'IndianAPI': it shows the unverified mark on the Desk and the Screener, like any figure no filing has confirmed.
"""
import calendar
import json
import math
import re
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from . import db, market

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://stock.indianapi.in"
SOURCE = "IndianAPI"
REF = "https://indianapi.in/indian-stock-market"
FILING = {"NSE XBRL", "BSE PDF", "BSE PDF (OCR)", "NSE PDF", "NSE PDF (OCR)", "BSE PDF (checked)", "Bank PDF"}
EXPECTED_QUARTER = "2026-06-30"
SALES_TOL = 0.02          # IndianAPI rounds to whole crore, so also allow 1 crore
OP_TOL_PP = 3.0           # operating profit may differ by this many points of sales (same rule as stitch.disagrees)


IA_DB = ROOT / "data" / "ia.db"
IA_SCHEMA = """
CREATE TABLE IF NOT EXISTS ia_stat (sym TEXT, stat TEXT, fetched TEXT, body TEXT, PRIMARY KEY (sym, stat));
CREATE TABLE IF NOT EXISTS ia_usage (month TEXT PRIMARY KEY, calls INTEGER);
"""


def _con():
    """The downloader's connection. Its own tables (statements as sent, call counts) live in data/ia.db, in write-ahead mode; the main database is attached as `m` for
    everything else (ia_fetch, quarter, stock...). A name that exists only in the main database resolves there; ia_stat and ia_usage resolve to ia.db. So a long
    write by the nightly or weekly job can no longer make a download fail with 'database is locked' (it did, on the first Saturday run)."""
    import sqlite3
    db.connect().close()                                            # makes sure the main database and its tables exist
    IA_DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(IA_DB, timeout=180)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(IA_SCHEMA)
    con.execute("ATTACH DATABASE ? AS m", (str(db.DB_PATH),))
    if con.execute("SELECT COUNT(*) FROM main.ia_stat").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM m.ia_stat").fetchone()[0]:     # one-time move
        con.execute("INSERT OR IGNORE INTO main.ia_stat SELECT * FROM m.ia_stat")
        con.execute("INSERT OR IGNORE INTO main.ia_usage SELECT * FROM m.ia_usage")
        con.commit()
    return con


def key():
    k = os.environ.get("INDIANAPI_KEY")
    env = ROOT / ".env"
    if not k and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("INDIANAPI_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        raise SystemExit("INDIANAPI_KEY is empty: paste the key after the = in .env")
    return k


def monthly_limit():
    return int(os.environ.get("INDIANAPI_MONTHLY_LIMIT", "500"))


def used(con):
    r = con.execute("SELECT calls FROM ia_usage WHERE month=?", (datetime.now().strftime("%Y-%m"),)).fetchone()
    return r[0] if r else 0


def _count(con, n=1):
    m = datetime.now().strftime("%Y-%m")
    con.execute("INSERT INTO ia_usage VALUES(?,?) ON CONFLICT(month) DO UPDATE SET calls=calls+?", (m, n, n))
    con.commit()


def _call(con, path, params):
    """One HTTP call, counted. Returns (status code, json or text)."""
    for attempt in range(5):                          # a sleeping laptop or a dropped network connection: wait and try again
        try:
            r = requests.get(BASE + path, params=params, headers={"x-api-key": key()}, timeout=60)
            break
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(15 * (attempt + 1))
    _count(con)
    time.sleep(1.1)                                   # the free plan allows one request a second
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def month_end(label):
    d = datetime.strptime(label, "%b %Y")
    return f"{d.year}-{d.month:02d}-{calendar.monthrange(d.year, d.month)[1]:02d}"


def parse(body):
    """{qend: {sales, op, np}} from a quarter_results reply, or (None, why). Banks and lenders use other row names: not supported."""
    if not isinstance(body, dict):
        return None, "unexpected reply"
    if "Sales" not in body or "Operating Profit" not in body or "Net Profit" not in body:
        return None, "not a company-format statement (bank or lender?)" if "Revenue" in body else "rows missing"
    out = {}
    for lab, sales in body["Sales"].items():
        try:
            qe = month_end(lab)
        except ValueError:
            continue
        op, np_ = body["Operating Profit"].get(lab), body["Net Profit"].get(lab)
        if sales is None or op is None or np_ is None:
            continue
        out[qe] = {"sales": float(sales), "op": float(op), "np": float(np_)}
    return out, ""


def ours(con, sym):
    """Quarters we already hold and trust enough to compare against: not flagged, and not from IndianAPI itself."""
    return {r["qend"]: dict(r) for r in con.execute(
        "SELECT qend,sales,op,np,basis,source FROM quarter WHERE sym=? AND flags='' AND source NOT LIKE 'IndianAPI%' AND sales IS NOT NULL AND op IS NOT NULL AND np IS NOT NULL",
        (sym,))}


def check(ia, mine):
    """Compare IndianAPI's quarters with ours. Returns (status, detail, np_scale, basis)."""
    both = [q for q in sorted(ia) if q in mine]
    if len(both) < 2:
        return "no_overlap", f"{len(both)} quarters in common", 1.0, None
    ok_sales = [q for q in both if abs(ia[q]["sales"] - mine[q]["sales"]) <= max(SALES_TOL * abs(mine[q]["sales"]), 1.0)]
    if len(ok_sales) < max(2, round(0.75 * len(both))):
        return "mismatch", f"sales agree in only {len(ok_sales)} of {len(both)} common quarters (other company, or another basis)", 1.0, None
    if both[-1] not in ok_sales:                                  # the newest quarter in common must agree: otherwise the company restated (demerger, merger) and
        return "break", f"{both[-1]} differs (IndianAPI {ia[both[-1]]['sales']:.0f}, ours {mine[both[-1]]['sales']:.0f}): restated figures would break the series", 1.0, None
    gap = [abs(ia[q]["op"] - mine[q]["op"]) / max(abs(mine[q]["sales"]), 1.0) * 100 for q in ok_sales]
    if statistics.median(gap) > OP_TOL_PP:
        return "op_differs", f"operating profit differs by {statistics.median(gap):.1f} points of sales", 1.0, None
    basis = max({mine[q]["basis"] for q in ok_sales}, key=lambda b: sum(mine[q]["basis"] == b for q in ok_sales))
    ratios = [mine[q]["np"] / ia[q]["np"] for q in ok_sales if ia[q]["np"] >= 5 and mine[q]["np"] > 0]
    if len(ratios) >= 3:
        med = statistics.median(ratios)
        if all(abs(r / med - 1) <= 0.15 for r in ratios) and 0.6 <= med <= 1.03:
            return "ok", f"{len(ok_sales)} quarters agree; net profit x{med:.3f}", (1.0 if abs(med - 1) <= 0.02 else round(med, 4)), basis
        return "np_differs", f"attributable/total profit ratio is not steady ({min(ratios):.2f} to {max(ratios):.2f})", 1.0, None
    if all(abs(ia[q]["np"] - mine[q]["np"]) <= max(3.0, 0.12 * abs(mine[q]["np"])) for q in ok_sales):
        return "ok", f"{len(ok_sales)} quarters agree (small profits)", 1.0, basis
    return "np_differs", "net profit differs and there are too few quarters to scale it", 1.0, None


def _search_name(name):
    for suffix in (" Limited", " Ltd.", " Ltd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def confirm(con, query, sym):
    """(True, isin) when IndianAPI's company profile for `query` carries `sym` as its NSE code."""
    code, body = _call(con, "/stock", {"name": query})
    prof = body.get("companyProfile") if code == 200 and isinstance(body, dict) else None
    if not isinstance(prof, dict):
        return False, None
    return (prof.get("exchangeCodeNse") or "").upper() == sym, prof.get("isInId")


def admit(con, sym, isin):
    """Add a stock the screener never held (profile from Yahoo, like the filings route in widen.py). False if Yahoo has no profile."""
    from .sources import yahoo
    try:
        prof = yahoo.profile(sym)
    except Exception:
        return False
    if not prof or not prof.get("name"):
        return False
    con.execute("INSERT OR REPLACE INTO stock(sym,name,sector,isin,shares_cr,cap_employed,equity,debt,np_annual,updated,desk) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (sym, prof["name"], prof["industry"], isin, prof["shares_cr"], prof["cap_employed"], prof["equity"], prof["debt"], prof["np_annual"],
                 datetime.now().isoformat(timespec="seconds")))
    return True


def healthy(con):
    """One call for a company IndianAPI certainly knows. Its company search has gone down mid-run before ("Not a valid script_code" for every
    name), and an outage must never be recorded as 'no data' for the stocks asked while it lasted."""
    for name in ("Reliance", "Infosys", "Ather Energy"):                # a name can drop out of their index on its own; the search is down only if all fail
        code, body = _call(con, "/historical_stats", {"stock_name": name, "stats": "quarter_results"})
        if code == 200 and isinstance(body, dict) and "Sales" in body:
            return True
    return False


def fetch_one(con, sym):
    """One stock: search by name, compare with what we hold, store the quarters if they pass."""
    row = con.execute("SELECT name FROM stock WHERE sym=?", (sym,)).fetchone()
    name = row["name"] if row and row["name"] else None
    if not name:
        u = market.connect().execute("SELECT name FROM universe WHERE sym=?", (sym,)).fetchone()
        name = u[0] if u and u[0] else sym
    if used(con) >= monthly_limit():
        return "limit", "monthly call allowance used up"
    ia, why, used_q = None, "not asked", name
    for q in dict.fromkeys([_search_name(name), sym]):          # the full legal name often finds nothing; the symbol usually does
        code, body = _call(con, "/historical_stats", {"stock_name": q, "stats": "quarter_results"})
        if code == 429:
            return "limit", "HTTP 429"
        ia, why = parse(body) if code == 200 else (None, f"HTTP {code}")
        if ia:
            used_q = q
            break
    if ia is None or not ia:
        status, detail, scale, basis = "no_data", why or "empty", 1.0, None
    else:
        status, detail, scale, basis = check(ia, ours(con, sym))
        if status == "no_overlap":                              # nothing to compare with: ask for the company profile and read its NSE code
            same, isin = confirm(con, used_q, sym)
            if same:
                status, detail, basis = "confirmed", detail + "; NSE code confirmed, profit not checked", None
                if not con.execute("SELECT 1 FROM stock WHERE sym=?", (sym,)).fetchone() and not admit(con, sym, isin):
                    status, detail = "no_profile", "NSE code confirmed, but Yahoo has no profile for the stock"
            else:
                status, detail = "unconfirmed", detail + "; NSE code did not match"
    con.execute("INSERT OR REPLACE INTO ia_fetch(sym,name,fetched,status,detail,np_scale,basis,query) VALUES(?,?,?,?,?,?,?,?)",
                (sym, name, datetime.now().isoformat(timespec="seconds"), status, detail, scale, basis, used_q))
    con.execute("DELETE FROM ia_quarter WHERE sym=?", (sym,))
    if ia and status not in ("no_data", "unconfirmed", "no_profile"):                    # kept for the audit report even when the stock is not accepted
        con.executemany("INSERT INTO ia_quarter VALUES(?,?,?,?,?,?)",
                        [(sym, q, v["sales"], v["op"], round(v["np"] * scale, 1), v["np"]) for q, v in ia.items()])
    con.commit()
    return status, detail


def unit_slip(ours, theirs):
    """True when our sales figure is IndianAPI's times 10, 100, 1000 or their fractions (or zero where they have a figure): a lost or doubled unit."""
    if theirs is None or theirs <= 5:
        return False
    if ours is None or abs(ours) < 1e-9:
        return True
    if ours < 0:
        return False
    k = math.log10(theirs / ours)
    return any(abs(k - p) < 0.02 for p in (1, 2, 3, -1, -2, -3))


def apply(sym, con=None):
    """Write the stored IndianAPI quarters of `sym` into `quarter`: only where the stock has no row, or only a flagged one. Returns rows added."""
    con = con or _con()
    con.execute("DELETE FROM quarter WHERE sym=? AND source LIKE 'IndianAPI%'", (sym,))
    f = con.execute("SELECT * FROM ia_fetch WHERE sym=? AND status IN ('ok','confirmed')", (sym,)).fetchone()
    if not f:
        con.commit()
        return 0
    have = {r["qend"]: r for r in con.execute("SELECT qend, sales, flags, source FROM quarter WHERE sym=?", (sym,))}
    source = (SOURCE + " (profit unchecked)" if f["status"] == "confirmed"
              else SOURCE if abs((f["np_scale"] or 1) - 1) < 1e-9 else SOURCE + " (profit scaled)")
    n = 0
    for r in con.execute("SELECT * FROM ia_quarter WHERE sym=? ORDER BY qend", (sym,)):
        e = have.get(r["qend"])
        label = source
        if e is not None and not e["flags"]:
            # A clean row stays, with two exceptions found by the audit (an accepted stock agrees with us nearly everywhere, so the odd quarter is the error):
            # a lost or doubled unit in any source, and a Yahoo figure (unverified by any filing) whose sales differ from IndianAPI's.
            if unit_slip(e["sales"], r["sales"]):
                label = SOURCE + " (corrects a unit error)"
            elif e["source"] in ("Yahoo", "Yahoo (PDF disagreed)") and e["sales"] is not None and abs(r["sales"] - e["sales"]) > max(SALES_TOL * abs(e["sales"]), 1.0):
                label = SOURCE + " (differs from Yahoo)"
            else:
                continue
        con.execute("DELETE FROM quarter WHERE sym=? AND qend=?", (sym, r["qend"]))
        con.execute("INSERT INTO quarter(sym,qend,sales,op,np,eps,basis,source,filed,ref,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (sym, r["qend"], r["sales"], r["op"], r["np"], None, f["basis"] or "Consolidated", label, None, REF, ""))
        n += 1
    con.commit()
    return n


def recheck(con=None):
    """Re-run the agreement test on every accepted stock from the stored quarters (no calls), now that the series is judged at its join as well."""
    con = con or _con()
    out = []
    for f in con.execute("SELECT sym FROM ia_fetch WHERE status='ok'").fetchall():
        sym = f[0]
        ia = {r["qend"]: {"sales": r["sales"], "op": r["op"], "np": r["np_raw"]} for r in con.execute("SELECT * FROM ia_quarter WHERE sym=?", (sym,))}
        con.execute("DELETE FROM quarter WHERE sym=? AND source LIKE 'IndianAPI%'", (sym,))     # compare with what we held before IndianAPI
        status, detail, scale, basis = check(ia, ours(con, sym))
        con.execute("UPDATE ia_fetch SET status=?, detail=?, np_scale=?, basis=? WHERE sym=?", (status, detail, scale, basis, sym))
        if status != "ok":
            con.execute("DELETE FROM ia_quarter WHERE sym=?", (sym,))
            out.append((sym, status, detail))
    con.commit()
    return out


def apply_all(con=None):
    con = con or _con()
    return {s: n for s in [r[0] for r in con.execute("SELECT sym FROM ia_fetch WHERE status IN ('ok','confirmed')")] if (n := apply(s, con))}


def targets(con, min_mcap):
    """Companies (not banks or lenders) with a results gap, biggest first, that IndianAPI has not been asked about yet."""
    from . import gaps
    asked = {r[0] for r in con.execute("SELECT sym FROM ia_fetch")}
    inbase = {r["sym"] for r in con.execute("SELECT sym FROM stock")}
    out = []
    for r in gaps.build():
        g = set(r["gaps"]) & {"stale", "few_quarters", "no_quarters"}
        if r["kind"] != "company" or not g or r["sym"] in asked or r["sym"] not in inbase or (r["mcap_cr"] or 0) < min_mcap:
            continue
        out.append(r)
    return sorted(out, key=lambda r: -(r["mcap_cr"] or 0))


def targets_all(con, min_mcap=0.0):
    """Every company already in the screener that IndianAPI has not been asked about, biggest first (the audit pass)."""
    asked = {r[0] for r in con.execute("SELECT sym FROM ia_fetch")}
    cap = dict(market.connect().execute("SELECT sym, mcap FROM class").fetchall())
    rows = con.execute("SELECT sym FROM stock WHERE COALESCE(kind,'corp')='corp'").fetchall()
    out = [{"sym": r[0], "mcap_cr": round(cap.get(r[0]) or 0)} for r in rows if r[0] not in asked and (cap.get(r[0]) or 0) >= min_mcap]
    return sorted(out, key=lambda r: -r["mcap_cr"])


def parse_annual(body):
    """{label: {sales, op, np}} for the financial years in a yoy_results reply (the trailing-twelve-months column is left out)."""
    if not isinstance(body, dict) or "Sales" not in body or "Net Profit" not in body:
        return {}
    out = {}
    for lab, sales in body["Sales"].items():
        np_ = body["Net Profit"].get(lab)
        if not re.fullmatch(r"[A-Z][a-z]{2} \d{4}", lab) or sales is None or np_ is None:      # TTM and part-year columns ("Mar 2025  9m") are not financial years
            continue
        out[lab] = {"sales": float(sales), "op": body.get("Operating Profit", {}).get(lab), "np": float(np_)}
    return out


def fetch_annual(con, sym):
    """One call: the yearly results of an accepted stock."""
    f = con.execute("SELECT query, name, np_scale FROM ia_fetch WHERE sym=? AND status IN ('ok','confirmed')", (sym,)).fetchone()
    if not f:
        return "skipped", "not an accepted stock"
    query = f["query"] or _search_name(f["name"] or sym)                  # stocks asked before the query was recorded
    if used(con) >= monthly_limit():
        return "limit", "monthly call allowance used up"
    code, body = _call(con, "/historical_stats", {"stock_name": query, "stats": "yoy_results"})
    if code == 429:
        return "limit", "HTTP 429"
    ann = parse_annual(body) if code == 200 else {}
    con.execute("DELETE FROM ia_annual WHERE sym=?", (sym,))
    con.executemany("INSERT INTO ia_annual VALUES(?,?,?,?,?)", [(sym, k, v["sales"], v["op"], v["np"]) for k, v in ann.items()])
    con.commit()
    return ("ok", f"{len(ann)} years") if ann else ("no_data", "empty")


def apply_annual(sym, con=None):
    """np_annual (newest first, attributable profit) from IndianAPI's yearly profit, scaled like the quarters, only where Yahoo's list has under four years."""
    con = con or _con()
    s = con.execute("SELECT np_annual FROM stock WHERE sym=?", (sym,)).fetchone()
    have = [x for x in str(s["np_annual"] or "").split(",") if x not in ("", "None")] if s else []
    f = con.execute("SELECT np_scale FROM ia_fetch WHERE sym=? AND status IN ('ok','confirmed')", (sym,)).fetchone()
    if not s or not f or len(have) >= 4:
        return False
    rows = sorted(con.execute("SELECT label, np FROM ia_annual WHERE sym=?", (sym,)), key=lambda r: datetime.strptime(r["label"], "%b %Y"), reverse=True)
    if len(rows) < 4:
        return False
    con.execute("UPDATE stock SET np_annual=? WHERE sym=?", (",".join(str(round(r["np"] * (f["np_scale"] or 1), 1)) for r in rows[:4]), sym))
    con.commit()
    return True


STAT_STATUSES = ("ok", "confirmed", "np_differs", "op_differs", "break", "stats_only")     # the company is the right one (the quarterly check may still have failed)
WIDE_SQL = "status IN " + str(STAT_STATUSES)
STATS = ("balancesheet", "cashflow", "ratios", "shareholding_pattern_quarterly", "shareholding_pattern_yearly", "profit_loss_stats")
ALL_KEYS = ("quarter_results", "yoy_results") + STATS              # what stats=all returns, all of it in ONE call: every one of these is stored, so no stock is ever asked twice


def fetch_all(con, sym):
    """One call with stats=all: every statement of an accepted stock (about 6 times cheaper than asking for each), each kept in ia_stat as sent."""
    f = con.execute("SELECT query, name FROM ia_fetch WHERE sym=? AND status IN ('ok','confirmed','np_differs','op_differs','break','stats_only')", (sym,)).fetchone()
    if not f:
        return "skipped", "not an accepted stock"
    query = f["query"] or _search_name(f["name"] or sym)
    if used(con) >= monthly_limit():
        return "limit", "monthly call allowance used up"
    for q in dict.fromkeys([query, sym]):                       # the full legal name often finds nothing; the symbol usually does
        code, body = _call(con, "/historical_stats", {"stock_name": q, "stats": "all"})
        if code == 429:
            return "limit", "HTTP 429"
        if code == 200 and isinstance(body, dict) and body and "info" not in body and "error" not in body:
            if q != query:
                con.execute("UPDATE ia_fetch SET query=? WHERE sym=?", (q, sym))
            break
    else:
        return "no_data", str(body)[:60]
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for k, v in body.items():
        if k in ALL_KEYS and isinstance(v, dict) and v:
            con.execute("INSERT OR REPLACE INTO ia_stat VALUES(?,?,?,?)", (sym, k, now, json.dumps(v, separators=(",", ":"))))
            n += 1
    con.commit()
    return ("ok", f"{n} statements") if n else ("no_data", "no statements in the reply")


def fetch_all_unasked(con, sym):
    """A stock IndianAPI was never asked about (banks, lenders, insurers): one `all` call by name (symbol as the fallback), accepted when its quarterly net profit agrees
    with ours for most common quarters. Stores the statements only; our quarterly rows are untouched."""
    row = con.execute("SELECT name FROM stock WHERE sym=?", (sym,)).fetchone()
    name = row["name"] if row and row["name"] else sym
    mine = {r["qend"]: r["np"] for r in con.execute("SELECT qend,np FROM quarter WHERE sym=? AND np IS NOT NULL", (sym,))}
    for q in dict.fromkeys([_search_name(name), sym]):
        if used(con) >= monthly_limit():
            return "limit", "monthly call allowance used up"
        code, body = _call(con, "/historical_stats", {"stock_name": q, "stats": "all"})
        if code == 429:
            return "limit", "HTTP 429"
        if code == 200 and isinstance(body, dict) and isinstance(body.get("quarter_results"), dict):
            break
    else:
        return "no_data", "not found by name or symbol"
    qr = body["quarter_results"]
    npr = qr.get("Net Profit") or {}
    common = [(month_end(l), v) for l, v in npr.items() if v is not None and month_end(l) in mine]
    good = [1 for qe, v in common if abs(v - mine[qe]) <= max(0.03 * abs(mine[qe]), 2.0)]
    if len(common) < 2 or len(good) < max(2, round(0.7 * len(common))):
        same, _isin = confirm(con, q, sym)                      # a lender's attributable profit can differ from Yahoo's: the NSE code settles whether it is the right company
        if not same:
            con.execute("INSERT OR REPLACE INTO ia_fetch(sym,name,fetched,status,detail,query) VALUES(?,?,?,?,?,?)",
                        (sym, name, datetime.now().isoformat(timespec="seconds"), "unconfirmed", f"net profit agrees in {len(good)} of {len(common)} common quarters; NSE code did not match", q))
            con.commit()
            return "unconfirmed", f"net profit agrees in {len(good)} of {len(common)} common quarters; NSE code did not match"
        good = common                                           # recorded below as confirmed by NSE code
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("INSERT OR REPLACE INTO ia_fetch(sym,name,fetched,status,detail,np_scale,basis,query) VALUES(?,?,?,?,?,?,?,?)",
                (sym, name, now, "stats_only", f"statements only; net profit agrees in {len(good)} of {len(common)} quarters", 1.0, None, q))
    n = 0
    for k, v in body.items():
        if k in ALL_KEYS and isinstance(v, dict) and v:
            con.execute("INSERT OR REPLACE INTO ia_stat VALUES(?,?,?,?)", (sym, k, now, json.dumps(v, separators=(",", ":"))))
            n += 1
    con.commit()
    return "ok", f"{n} statements, net profit agrees in {len(good)} of {len(common)} quarters"


def fetch_stat(con, sym, stat):
    """One call: one statement of an accepted stock, kept as IndianAPI sent it (rows by label, columns by period) in ia_stat."""
    f = con.execute("SELECT query, name FROM ia_fetch WHERE sym=? AND status IN ('ok','confirmed','np_differs','op_differs','break','stats_only')", (sym,)).fetchone()
    if not f:
        return "skipped", "not an accepted stock"
    query = f["query"] or _search_name(f["name"] or sym)
    if used(con) >= monthly_limit():
        return "limit", "monthly call allowance used up"
    code, body = _call(con, "/historical_stats", {"stock_name": query, "stats": stat})
    if code == 429:
        return "limit", "HTTP 429"
    if code != 200 or not isinstance(body, dict) or "info" in body or "error" in body or not body:
        return "no_data", str(body)[:60]
    con.execute("INSERT OR REPLACE INTO ia_stat VALUES(?,?,?,?)", (sym, stat, datetime.now().isoformat(timespec="seconds"), json.dumps(body, separators=(",", ":"))))
    con.commit()
    return "ok", f"{len(body)} rows"


def audit_report(con, path):
    """Every quarter where IndianAPI and our row differ by more than rounding, with our source: the list to look through."""
    import csv
    rows = []
    for r in con.execute("""SELECT i.sym, i.qend, i.sales ia_s, i.op ia_o, i.np_raw ia_n, q.sales s, q.op o, q.np n, q.source, q.basis, f.status, f.np_scale
                            FROM ia_quarter i JOIN quarter q ON q.sym=i.sym AND q.qend=i.qend JOIN ia_fetch f ON f.sym=i.sym
                            WHERE q.source NOT LIKE 'IndianAPI%' AND q.flags='' AND q.sales IS NOT NULL AND q.op IS NOT NULL AND q.np IS NOT NULL"""):
        sc = r["np_scale"] or 1.0
        dif = []
        if abs(r["ia_s"] - r["s"]) > max(SALES_TOL * abs(r["s"]), 1.0):
            dif.append("sales")
        if abs(r["ia_o"] - r["o"]) / max(abs(r["s"]), 1.0) * 100 > OP_TOL_PP:
            dif.append("operating profit")
        if r["status"] == "ok" and abs(r["ia_n"] * sc - r["n"]) > max(5.0, 0.15 * abs(r["n"])):
            dif.append("net profit")
        if dif:
            rows.append([r["sym"], r["qend"], r["status"], r["source"], r["s"], r["ia_s"], r["o"], r["ia_o"], r["n"], round(r["ia_n"] * sc, 1), "; ".join(dif)])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "quarter", "indianapi_status", "our_source", "our_sales", "ia_sales", "our_op", "ia_op", "our_np", "ia_np_scaled", "differs_in"])
        w.writerows(sorted(rows))
    return len(rows)


def report(con):
    """Stocks in the NSE universe that the screener never loaded (Yahoo had nothing), biggest first; banks and lenders left out."""
    import json
    from . import widen
    skipped = json.loads(widen.SKIPPED.read_text(encoding="utf-8")) if widen.SKIPPED.exists() else {}
    asked = {r[0] for r in con.execute("SELECT sym FROM ia_fetch")}
    have = {r[0] for r in con.execute("SELECT sym FROM stock")}
    rows = market.connect().execute("SELECT u.sym, c.mcap FROM universe u JOIN class c ON c.sym=u.sym WHERE c.mcap >= ? ORDER BY c.mcap DESC", (min_mcap,)).fetchall()
    return [{"sym": r[0], "mcap_cr": round(r[1])} for r in rows if r[0] not in have and r[0] not in asked and skipped.get(r[0]) != "financial company"]


def report(con):
    rows = con.execute("SELECT status, COUNT(*) n FROM ia_fetch GROUP BY status ORDER BY n DESC").fetchall()
    print("Stocks asked about:", ", ".join(f"{r['status']} {r['n']}" for r in rows) or "none")
    print(f"Calls used this month: {used(con)} of {monthly_limit()}")
    for r in con.execute("SELECT * FROM ia_fetch WHERE status!='ok' ORDER BY status, sym"):
        print(f"  {r['sym']:<12} {r['status']:<10} {r['detail']}")
    n = con.execute("SELECT COUNT(DISTINCT sym) FROM quarter WHERE source LIKE 'IndianAPI%'").fetchone()[0]
    print(f"Stocks now holding IndianAPI quarters: {n}")


def main(argv):
    con = _con()
    mc = float(argv[argv.index("--min-mcap") + 1]) if "--min-mcap" in argv else 500.0
    if "--report" in argv:
        return report(con)
    if "--health" in argv:                                      # exit code 0 when the company search answers (one call)
        ok = healthy(con)
        print("IndianAPI search: " + ("up" if ok else "down"))
        sys.exit(0 if ok else 1)
    if "--recheck" in argv:
        for r in recheck(con):
            print(*r)
    if "--apply" in argv or "--recheck" in argv:
        print(apply_all(con))
        return
    if "--plan" in argv:
        t = targets_new(con, mc) if "--new" in argv else targets_all(con, mc) if "--all" in argv else targets(con, mc)
        print(f"{len(t)} stocks at or above {mc:.0f} Cr have a gap and have not been asked yet; calls used {used(con)} of {monthly_limit()}")
        for r in t[:40]:
            print(f"  {r['sym']:<12} {(r['mcap_cr'] or 0):>9,} Cr  {', '.join(r.get('gaps', ['company']))}")
        return
    if "--retry" in argv:                                       # ask again about stocks that came back with no data
        con.execute("DELETE FROM ia_fetch WHERE status='no_data'")
        con.commit()
    if "--sym" in argv:
        syms = [s.strip().upper() for s in argv[argv.index("--sym") + 1].split(",") if s.strip()]
    elif "--annual" in argv:                                    # python -m backend.indianapi --annual 500: yearly results of accepted stocks, those short of annual profit first
        n = int(argv[argv.index("--annual") + 1])
        done = {r[0] for r in con.execute("SELECT DISTINCT sym FROM ia_annual")}
        short = {r["sym"] for r in con.execute("SELECT sym, np_annual FROM stock") if len([x for x in str(r["np_annual"] or "").split(",") if x not in ("", "None")]) < 4}
        todo = [r[0] for r in con.execute("SELECT sym FROM ia_fetch WHERE status IN ('ok','confirmed')") if r[0] not in done]
        todo = sorted(todo, key=lambda s: s not in short)[:n]
        filled = 0
        for i, sym in enumerate(todo):
            if i % 50 == 0 and not healthy(con):
                print("IndianAPI's company search is not answering: stopping. Run the same command again later.", flush=True)
                break
            status, detail = fetch_annual(con, sym)
            filled += apply_annual(sym, con) if status == "ok" else 0
            print(f"{sym:<12} {status:<8} {detail}", flush=True)
            if status == "limit":
                break
        print(f"np_annual filled for {filled} stocks; calls used this month: {used(con)} of {monthly_limit()}")
        return
    elif "--banks" in argv:                                     # python -m backend.indianapi --banks --max-calls 300: banks, lenders and insurers (never asked before)
        cap = int(argv[argv.index("--max-calls") + 1]) if "--max-calls" in argv else 0
        if not cap:
            print("give --max-calls N")
            return
        asked = {r[0] for r in con.execute("SELECT sym FROM ia_fetch")}
        todo = [r[0] for r in con.execute("SELECT sym FROM stock WHERE kind='fin' ORDER BY sym") if r[0] not in asked]
        print(f"{len(todo)} banks, lenders and insurers to ask about; this run stops after {cap} calls", flush=True)
        start, n_ok = used(con), 0
        for i, sym in enumerate(todo):
            if used(con) - start >= cap:
                break
            if i % 50 == 0 and not healthy(con):
                print("IndianAPI's company search is not answering: stopping.", flush=True)
                break
            status, detail = fetch_all_unasked(con, sym)
            n_ok += status == "ok"
            print(f"{sym:<12} {status:<11} {detail}", flush=True)
            if status == "limit":
                break
        print(f"stored {n_ok}; calls used this month (my count): {used(con)}")
        return
    elif "--stats" in argv:                                     # python -m backend.indianapi --stats balancesheet,cashflow --max-calls 1000
        want_all = argv[argv.index("--stats") + 1] == "all"
        wanted = ["yoy_results"] if want_all else [x for x in argv[argv.index("--stats") + 1].split(",") if x in STATS]
        cap = int(argv[argv.index("--max-calls") + 1]) if "--max-calls" in argv else 0
        if not wanted or not cap:
            print("give --stats all (every statement in one call per stock) or any of", ", ".join(STATS), "and --max-calls N (the most calls this run may make)")
            return
        cap_cr = {k: v for k, v in market.connect().execute("SELECT sym, mcap FROM class")}
        accepted = sorted((r[0] for r in con.execute("SELECT sym FROM ia_fetch WHERE " + WIDE_SQL)), key=lambda s: -(cap_cr.get(s) or 0))
        done = {(r[0], r[1]) for r in con.execute("SELECT sym, stat FROM ia_stat")}
        todo = [(s, st) for st in wanted for s in accepted if (s, st) not in done]
        start, n_ok, i = used(con), 0, 0
        print(f"{len(todo)} downloads to do; this run stops after {cap} calls", flush=True)
        for sym, st in todo:
            if used(con) - start >= cap:
                break
            if i % 50 == 0 and not healthy(con):
                print("IndianAPI's company search is not answering: stopping. Run the same command again later.", flush=True)
                break
            i += 1
            status, detail = fetch_all(con, sym) if want_all else fetch_stat(con, sym, st)
            n_ok += status == "ok"
            if status == "limit":
                print("allowance used up", flush=True)
                break
            if i % 25 == 0:
                print(f"  {i} calls, {n_ok} stored ({used(con) - start} counted)", flush=True)
        left = len([1 for t in todo if t not in {(r[0], r[1]) for r in con.execute('SELECT sym, stat FROM ia_stat')}])
        print(f"stored {n_ok}; {left} downloads still to do; calls used this month (my count): {used(con)}")
        return
    elif "--audit-report" in argv:
        path = ROOT / "data" / "ia_audit.csv"
        print(f"{audit_report(con, path)} differing quarters written to {path}")
        return
    elif "--fetch" in argv:
        pool = targets_new(con, mc) if "--new" in argv else targets_all(con, mc) if "--all" in argv else targets(con, mc)
        syms = [r["sym"] for r in pool][:int(argv[argv.index("--fetch") + 1])]
    else:
        print(__doc__)
        return
    failures = 0
    for i, sym in enumerate(syms):
        if i % 50 == 0 and not healthy(con):
            print("IndianAPI's company search is not answering (checked with Reliance): stopping so no stock is recorded as 'no data'. Run the same command again later.", flush=True)
            break
        try:
            status, detail = fetch_one(con, sym)
            failures = 0
        except Exception as e:                         # one stock failing must not stop the run; ten in a row means the network is down
            failures += 1
            print(f"{sym:<12} error      {type(e).__name__}: {e}", flush=True)
            if failures >= 10:
                print("ten failures in a row: stopping; run the same command again to carry on", flush=True)
                break
            time.sleep(30)
            continue
        added = apply(sym, con) if status in ("ok", "confirmed") else 0
        print(f"{sym:<12} {status:<10} {detail}" + (f"  (+{added} quarters)" if added else ""), flush=True)
        if status == "limit":
            break
    print(f"calls used this month: {used(con)} of {monthly_limit()}")


if __name__ == "__main__":
    main(sys.argv[1:])
