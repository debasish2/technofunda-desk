"""Saved screens and their daily "new matches".

A saved screen is a named set of filters (the same dictionary the screener page sends). The nightly job runs every saved
screen against the latest data and stores which stocks matched on which session (table screen_hit), so the screener page can
show what is NEW since the previous session instead of making you compare two lists by eye.

Screens carry a `user` column ('me' for now) so the table is ready for several people without a migration.
"""
import json
from datetime import datetime

from . import fundamentals
from . import indicators as ind


def match(filters, min_value_cr, snap, ft):
    """The stocks (a DataFrame, strongest RS first) that pass the filters, the way /api/screener/run applies them."""
    filters = dict(filters or {})
    if min_value_cr:
        filters["min_value_cr"] = min_value_cr
    fund = {k: v for k, v in filters.items() if k in fundamentals.FUND_IDS}
    tech = {k: v for k, v in filters.items() if k not in fundamentals.FUND_IDS}
    hit = ind.apply(snap, tech)
    if any(v not in (None, "", False) for v in fund.values()):
        hit = hit[hit.index.isin(fundamentals.apply(ft, fund).index)]
    return hit


def asof(snap):
    return str(snap["asof"].iloc[0])


def snapshot(con, sid, filters, min_value_cr, snap, ft):
    """Store today's matches for a screen; returns (session date, [symbols])."""
    d = asof(snap)
    syms = list(match(filters, min_value_cr, snap, ft).index)
    con.execute("DELETE FROM screen_hit WHERE screen_id=? AND d=?", (sid, d))
    con.executemany("INSERT INTO screen_hit(screen_id,d,sym) VALUES(?,?,?)", [(sid, d, s) for s in syms])
    con.commit()
    return d, syms


def latest_two(con, sid):
    """(newest snapshot date, its symbols, previous snapshot date, its symbols)."""
    ds = [r[0] for r in con.execute("SELECT DISTINCT d FROM screen_hit WHERE screen_id=? ORDER BY d DESC LIMIT 2", (sid,))]
    get = lambda d: [r[0] for r in con.execute("SELECT sym FROM screen_hit WHERE screen_id=? AND d=?", (sid, d))] if d else []
    d0 = ds[0] if ds else None
    d1 = ds[1] if len(ds) > 1 else None
    return d0, get(d0), d1, get(d1)


def summary(con, user="me"):
    out = []
    for r in con.execute("SELECT id,name,filters,min_value_cr,created FROM screen WHERE user=? ORDER BY id", (user,)):
        d0, now, d1, before = latest_two(con, r["id"])
        new = [s for s in now if s not in set(before)] if d1 else []
        out.append({"id": r["id"], "name": r["name"], "filters": json.loads(r["filters"]), "min_value_cr": r["min_value_cr"],
                    "created": r["created"], "asof": d0, "count": len(now), "new": new, "new_since": d1})
    return out


def create(con, name, filters, min_value_cr, snap, ft, user="me"):
    cur = con.execute("INSERT INTO screen(user,name,filters,min_value_cr,created) VALUES(?,?,?,?,?)",
                      (user, name.strip()[:60] or "Untitled", json.dumps(filters), float(min_value_cr or 0), datetime.now().isoformat(timespec="seconds")))
    con.commit()
    snapshot(con, cur.lastrowid, filters, min_value_cr, snap, ft)
    return cur.lastrowid


def remove(con, sid, user="me"):
    con.execute("DELETE FROM screen_hit WHERE screen_id IN (SELECT id FROM screen WHERE id=? AND user=?)", (sid, user))
    con.execute("DELETE FROM screen WHERE id=? AND user=?", (sid, user))
    con.commit()


def snapshot_all(con, snap, ft):
    n = 0
    for r in con.execute("SELECT id,filters,min_value_cr FROM screen").fetchall():
        snapshot(con, r["id"], json.loads(r["filters"]), r["min_value_cr"], snap, ft)
        n += 1
    return n
