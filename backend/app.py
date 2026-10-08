"""API + dashboard server: uvicorn backend.app:app --reload"""
import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, quality

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="Setup Desk")
app.add_middleware(GZipMiddleware, minimum_size=1200)       # the price history and screener results are large JSON
app.mount("/vendor", StaticFiles(directory=ROOT / "vendor"), name="vendor")      # third-party libraries, kept local


def stock_payload(con, row, with_bars=True):
    sym = row["sym"]
    s = {"sym": sym, "name": row["name"], "sector": row["sector"] or "Unclassified",
         "shares": row["shares_cr"], "capEmployed": row["cap_employed"], "equity": row["equity"]}
    if with_bars:
        s["bars"] = [dict(d=r["d"], o=r["o"], h=r["h"], l=r["l"], c=r["c"], v=r["v"])
                     for r in con.execute("SELECT * FROM bar WHERE sym=? ORDER BY d", (sym,))]
        if not s["bars"]:                                  # a screener-only stock: its prices live in market.db
            s["bars"] = [dict(d=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in market.connect().execute(
                "SELECT d, o, h, l, c, v FROM mbar WHERE sym=? ORDER BY d", (sym,))]
    # only figures that passed their checks reach the dashboard; a flagged quarter is a gap, not a guess
    qrows = con.execute("SELECT * FROM quarter WHERE sym=? AND flags='' AND sales IS NOT NULL AND op IS NOT NULL "
                        "AND np IS NOT NULL ORDER BY qend DESC LIMIT 12", (sym,)).fetchall()
    ver = quality.for_stock(con, sym, qrows)                # f: from a filing, m: Yahoo confirmed by a filing, u: Yahoo only
    s["quarters"] = [dict(end=r["qend"], sales=r["sales"], op=r["op"], np=r["np"], eps=r["eps"],
                          source=r["source"], filed=r["filed"], ver=ver[r["qend"]]) for r in qrows][::-1]
    # insider trades (last 12 months), promoter holding and pledge, from NSE's disclosure feeds
    since = (date.today() - timedelta(days=365)).isoformat()
    s["discLoaded"] = con.execute("SELECT 1 FROM disc_fetch WHERE sym=?", (sym,)).fetchone() is not None
    s["insider"] = [dict(d=r["d"], who=r["who"], cat=r["category"], type=r["kind"], mode=r["mode"], qty=r["qty"], val=r["val_cr"])
                    for r in con.execute("SELECT * FROM insider WHERE sym=? AND d>=? ORDER BY d DESC", (sym, since))]
    hold = [dict(q=r["qend"], pct=r["promoter_pct"]) for r in con.execute(
        "SELECT qend, promoter_pct FROM holding WHERE sym=? ORDER BY qend DESC LIMIT 8", (sym,))]
    pl = con.execute("SELECT * FROM pledge WHERE sym=? ORDER BY asof DESC LIMIT 1", (sym,)).fetchone()
    s["cred"] = None
    if hold or pl:
        year_ago = hold[4]["pct"] if len(hold) > 4 else None
        s["cred"] = {"real": True, "promoPct": hold[0]["pct"] if hold else None, "asof": hold[0]["q"] if hold else None,
                     "promoChg": round(hold[0]["pct"] - year_ago, 2) if hold and year_ago is not None else None,
                     "series": hold, "pledge": pl["pledged_pct_of_promoter"] if pl else None,
                     "pledgeTotal": pl["pledged_pct_of_total"] if pl else None, "pledgeAsof": pl["asof"] if pl else None}
    mcon = market.connect()
    a = mcon.execute("SELECT kind, stage FROM asm WHERE sym=?", (sym,)).fetchone()
    s["asm"] = {"kind": a[0], "stage": a[1]} if a else None
    last_bar = s["bars"][-1]["d"] if with_bars and s.get("bars") else "9999-12-31"
    dv = [r[0] for r in mcon.execute("SELECT pct FROM deliv WHERE sym=? AND d<=? ORDER BY d DESC LIMIT 5", (sym, last_bar))]
    s["deliv"] = {"pct": dv[0], "avg5": round(sum(dv) / len(dv), 1)} if dv else None
    snap = market_tables()["snap"]
    s["tech"] = None
    s["industry"] = None
    if sym in snap.index:
        r = snap.loc[sym]
        s["tech"] = {k: clean(r[k]) for k in ("rs", "rs1m", "rs3m", "rs6m", "rs12m", "stage", "template", "mansfield", "momentum")}
        if isinstance(r.get("industry"), str):
            s["industry"] = {"macro": r["macro"], "sector": r["sector"], "industry": r["industry"], "basic": r["basic"],
                             **{k: clean(r[k]) for k in ("ind_1w", "ind_1m", "ind_3m", "ind_rank_1m", "ind_rank_3m", "ind_groups")}}
    return s


_desk = {"key": None, "json": ""}


def desk_json():
    """The Desk's stocks as one JSON string. Building it reads every price bar, so keep it until the data underneath changes."""
    c = market_tables()
    key = (db.DB_PATH.stat().st_mtime, c["stamp"], c.get("ctok"))
    if _desk["key"] != key:
        con = db.connect()
        rows = [stock_payload(con, r) for r in con.execute("SELECT * FROM stock WHERE desk=1 ORDER BY sym")]
        _desk.update(key=key, json=json.dumps(rows, separators=(",", ":")) if rows else "", etag='"%x"' % (hash(key) & 0xFFFFFFFF))
    return _desk


@app.get("/api/stocks")
def stocks(request: Request):
    d = desk_json()
    if request.headers.get("if-none-match") == d["etag"]:
        return Response(status_code=304, headers={"ETag": d["etag"]})
    return Response(d["json"] or "[]", media_type="application/json", headers={"ETag": d["etag"], "Cache-Control": "no-cache"})


@app.get("/api/history/{sym}")
def history_bars(sym: str):
    """The stock's whole price history (adjusted daily bars) for the chart: [[date, o, h, l, c, v], ...]."""
    from . import history
    return {"sym": sym, "bars": history.get(sym)}


@app.get("/api/stocks/{sym}")
def one(sym: str):
    con = db.connect()
    sym = sym.upper()
    r = con.execute("SELECT * FROM stock WHERE sym=?", (sym,)).fetchone()
    if not r:
        u = market.connect().execute("SELECT name FROM universe WHERE sym=?", (sym,)).fetchone()
        if not u:
            raise HTTPException(404, "unknown symbol")
        r = {"sym": sym, "name": u[0], "sector": None, "shares_cr": None, "cap_employed": None, "equity": None, "kind": "corp"}
    return stock_payload(con, r)


@app.get("/api/quotes")
def quotes(syms: str):
    """Last price, day change, volume and surveillance flag for a list of symbols (the Desk's Screener list)."""
    want = [x for x in syms.upper().split(",") if x][:600]
    snap = market_tables()["snap"]
    mcon = market.connect()
    ph = ",".join("?" * len(want))
    vol = {r[0]: r[1] for r in mcon.execute(
        f"SELECT sym, v FROM mbar WHERE sym IN ({ph}) AND (sym, d) IN (SELECT sym, MAX(d) FROM mbar GROUP BY sym)", want)}
    asm = {r[0]: r[1] for r in mcon.execute(f"SELECT sym, stage FROM asm WHERE sym IN ({ph})", want)}
    out = []
    for s in want:
        if s in snap.index:
            r = snap.loc[s]
            out.append({"sym": s, "name": r["name"], "last": clean(r["last"]), "chg": clean(r["chg"]), "vol": vol.get(s), "asm": asm.get(s)})
    return out


@app.get("/api/company/{sym}")
def api_company(sym: str, refresh: int = 0):
    """About, annual statements, ratios, shareholding and documents for the Fundamentals tab (see backend/company.py)."""
    from . import company
    sym = sym.upper()
    try:
        return company.get(sym, refresh=bool(refresh))
    except Exception as e:
        raise HTTPException(502, f"could not load company details: {type(e).__name__}: {e}")


@app.get("/api/company/{sym}/standalone")
def api_company_standalone(sym: str, refresh: int = 0):
    """Standalone quarters and financial years for the Consolidated / Standalone switch (first call for a stock takes up to a minute)."""
    from . import company
    try:
        return company.standalone(sym.upper(), refresh=bool(refresh))
    except Exception as e:
        raise HTTPException(502, f"could not load standalone figures: {type(e).__name__}: {e}")


@app.get("/api/company/{sym}/years")
def api_company_years(sym: str, refresh: int = 0):
    """Ten financial years, consolidated and standalone, for the Desk's 10 Years tab (first call for a stock takes up to a minute)."""
    from . import annual
    try:
        return annual.get(sym.upper(), refresh=bool(refresh))
    except Exception as e:
        raise HTTPException(502, f"could not load the ten-year figures: {type(e).__name__}: {e}")


@app.get("/data.js")
def data_js(request: Request):
    """The dashboard loads this; it replaces the generated sample data."""
    d = desk_json()
    if request.headers.get("if-none-match") == d["etag"][:-1] + 'j"':
        return Response(status_code=304, headers={"ETag": d["etag"][:-1] + 'j"'})
    body = "window.SETUP_DESK_DATA = " + d["json"] + ";\n" if d["json"] else ""
    return PlainTextResponse(body, media_type="application/javascript", headers={"Cache-Control": "no-cache", "ETag": d["etag"][:-1] + 'j"'})


@app.get("/")
def index():
    return FileResponse(ROOT / "setup-desk.html")


# ---------------------------------------------------------------- market-wide: breadth + technical screener
import math  # noqa: E402

from fastapi import Body  # noqa: E402

from . import fundamentals  # noqa: E402
from . import indicators as ind  # noqa: E402
from . import market  # noqa: E402

_cache = {}


def market_tables():
    """Indicator tables for the whole market, rebuilt only when market.db has changed."""
    # keyed on the "loaded" marker the refresh writes LAST, so a half-finished nightly run is never served
    stamp = (market.connect().execute("SELECT v FROM meta WHERE k='loaded'").fetchone() or [None])[0]
    if _cache.get("stamp") != stamp or "snap0" not in _cache:
        data = ind.load()
        _cache.clear()
        _cache.update(stamp=stamp, data=data, snap0=ind.snapshot(data), breadth=ind.breadth(data))
    from . import industries
    tok = industries.token()
    if _cache.get("ctok") != tok or "snap" not in _cache:       # industry names / ranks ride along on the snapshot
        _cache.update(snap=industries.enrich(_cache["snap0"], _cache["data"]), ctok=tok)
    return _cache


def clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return None if (isinstance(v, float) and math.isnan(v)) else v


_bd = {"key": None, "warm": None}


def breadth_ctx():
    """The price matrices the breadth pages use: the stored history, plus today's provisional row while backend.live has a fresh one.
    Series computed from them are cached here and dropped whenever the underlying data (or the live snapshot) changes."""
    c = market_tables()
    con = market.connect()
    meta = dict(con.execute("SELECT k, v FROM meta WHERE k IN ('live_d','live_asof','live_n','live_idx')").fetchall())
    last = str(c["data"][0]["c"].index[-1])
    live = None
    if meta.get("live_d", "") > last and int(meta.get("live_n") or 0) >= 0.8 * int(c["data"][0]["c"].iloc[-1].notna().sum()):
        live = {"d": meta["live_d"], "asof": meta["live_asof"], "n": int(meta["live_n"])}
    key = (c["stamp"], live and live["asof"])
    if _bd["key"] != key:
        data = c["data"]
        if live:
            rows = con.execute("SELECT sym,o,h,l,c,v FROM live_bar").fetchall()
            data = ind.with_live(data, rows, float(meta["live_idx"]), live["d"])
        _bd.update(key=key, data=data, live=live, mapct={}, mbi={}, breadth=None)
    return _bd


@app.get("/api/breadth")
def api_breadth():
    bd = breadth_ctx()
    if bd["breadth"] is None:
        bd["breadth"] = ind.breadth(bd["data"])
    b = bd["breadth"]
    cols = ["advancing", "declining", "up4", "down4", "traded", "above50", "above200", "base50", "base200",
            "pct50", "pct200", "new_high", "new_low", "ad_line"]
    out = {"dates": list(b.index), "sessions": len(b), "live": bd["live"]}
    for c in cols:
        out[c] = [clean(float(x)) for x in b[c]]
    return out


def _jsonable(df, extra=None):
    out = dict(extra or {})
    out["dates"] = list(df.index)
    for col in df.columns:
        out[col] = [None if (v != v or v in (float("inf"), float("-inf"))) else round(float(v), 2) for v in df[col]]
    return out


def _warm():
    """Keep the breadth series ready: build them once the server is up and again whenever new data (a nightly load or a live
    snapshot) arrives, so a page never waits for the maths."""
    import time as _t
    while True:
        try:
            bd = breadth_ctx()
            if _bd["warm"] != bd["key"]:
                for kind in ("ema", "sma"):
                    api_breadth_ma(kind=kind, sessions=260, periods="5,10,20,21,30,50,100,150,200")
                api_breadth_mbi(kind="sma", thr=4.0, sessions=11)
                api_breadth()
                _bd["warm"] = bd["key"]
        except Exception as e:                              # warming is a courtesy; the next request will do the work
            print("warm-up skipped:", e)
        _t.sleep(60)


@app.on_event("startup")
def _startup():
    import threading
    threading.Thread(target=_warm, daemon=True).start()


@app.get("/api/breadth/ma")
def api_breadth_ma(kind: str = "ema", sessions: int = 260, periods: str = "10,20,50,200"):
    """% of stocks above their chosen moving averages (EMA or SMA), per session."""
    kind = "sma" if kind == "sma" else "ema"
    try:
        per = sorted({int(x) for x in periods.split(",") if x.strip()})[:8]
    except ValueError:
        raise HTTPException(400, "periods must be whole numbers")
    per = [n for n in per if 2 <= n <= 400] or [10, 20, 50, 200]
    bd = breadth_ctx()
    df = ind.ma_breadth(bd["data"], kind, 600, tuple(per), cache=bd["mapct"]).tail(max(20, min(sessions, 600)))
    return _jsonable(df, {"kind": kind, "periods": per, "live": bd["live"]})


@app.get("/api/breadth/mbi")
def api_breadth_mbi(kind: str = "sma", thr: float = 4.0, sessions: int = 40):
    """The market-breadth dashboard table (4R, 20R, 50R, their changes, new highs and lows), newest session last."""
    kind = "ema" if kind == "ema" else "sma"
    thr = 4.5 if thr >= 4.25 else 4.0
    bd = breadth_ctx()
    if (kind, thr) not in bd["mbi"]:
        bd["mbi"][(kind, thr)] = ind.mbi(bd["data"], kind, thr, 120)
    return _jsonable(bd["mbi"][(kind, thr)].tail(max(5, min(sessions, 120))), {"kind": kind, "thr": thr, "live": bd["live"]})


@app.get("/api/industries")
def api_industries(level: str = "industry", min_mcap: float = 500, min_n: int = 3, agg: str = "mean"):
    """Every industry group with its return over today / 1W / 1M / 3M / 6M / YTD / 1Y (see backend/industries.py)."""
    from . import industries
    if level not in industries.LEVELS:
        raise HTTPException(400, "level must be one of " + ", ".join(industries.LEVELS))
    bd = breadth_ctx()
    key = ("ind", level, min_mcap, min_n, agg)
    if key not in bd["mbi"]:
        try:
            rows = industries.groups(bd["data"], level, min_mcap, min_n, agg, market_tables()["snap"]["rs"])
        except Exception as e:
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        bd["mbi"][key] = rows
    rows = bd["mbi"][key]
    return {"level": level, "level_name": industries.LEVELS[level], "rows": rows, "stocks": sum(r["n"] for r in rows) if level != "theme" else None,
            "asof": str(bd["data"][0]["c"].index[-1]), "live": bd["live"]}


@app.get("/api/industries/members")
def api_industries_members(level: str, name: str, min_mcap: float = 0):
    from . import industries
    bd = breadth_ctx()
    return {"rows": industries.members(bd["data"], level, name, rs=market_tables()["snap"]["rs"], min_mcap=min_mcap)}


@app.get("/api/themes")
def api_themes_get():
    from . import industries
    return industries.load_themes()


@app.put("/api/themes")
def api_themes_put(body: dict = Body(...)):
    """Replace data/themes.json. Each theme: {"basic"|"industry"|"sector"|"macro": [names], "symbols": [...], "exclude": [...]}."""
    from . import industries
    import json as _json
    for name, rule in body.items():
        if not isinstance(name, str) or not isinstance(rule, dict) or any(not isinstance(v, list) for v in rule.values()):
            raise HTTPException(400, f"theme {name!r} must be an object whose values are lists")
    industries.THEMES.write_text(_json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    for k in [k for k in _bd.get("mbi", {}) if isinstance(k, tuple) and k and k[0] == "ind"]:
        _bd["mbi"].pop(k, None)
    return {"ok": True, "themes": len(body)}


@app.get("/industries")
def industries_page():
    return FileResponse(ROOT / "industries.html")


_fund = {}


def fund_table():
    """Fundamental metrics for the Desk's stocks, rebuilt when the Desk database changes."""
    stamp = db.DB_PATH.stat().st_mtime
    if _fund.get("stamp") != stamp:
        _fund.update(stamp=stamp, table=fundamentals.snapshot())
    return _fund["table"]


@app.get("/api/screener/fields")
def api_fields():
    from . import industries
    return ind.FIELDS + industries.fields() + fundamentals.FIELDS


@app.get("/api/screener/coverage")
def api_coverage():
    """How many stocks the fundamental filters can see, out of how many the technical ones can."""
    ft = fund_table()
    return {"fundamentals": int((~ft["stale"]).sum()), "stale": int(ft["stale"].sum()),
            "technical": int(len(market_tables()["snap"]))}


@app.post("/api/screener/run")
def api_run(body: dict = Body(...)):
    """body: {"filters": {field_id: value}, "min_value_cr": 0, "limit": 300}"""
    snap = market_tables()["snap"]
    filters = dict(body.get("filters") or {})
    if body.get("min_value_cr"):
        filters["min_value_cr"] = body["min_value_cr"]
    fund_filters = {k: v for k, v in filters.items() if k in fundamentals.FUND_IDS}
    tech_filters = {k: v for k, v in filters.items() if k not in fundamentals.FUND_IDS}
    fund_used = any(v not in (None, "", False) for v in fund_filters.values())
    ft = fund_table()
    try:
        hit = ind.apply(snap, tech_filters)
        if fund_used:                       # fundamentals exist only for the Desk's stocks, so only they can match
            hit = hit[hit.index.isin(fundamentals.apply(ft, fund_filters).index)]
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"bad filter: {e}")
    # how the screen narrows the field, stage by stage (the fundamental stage can only see the Desk's stocks)
    has_tech = any(v not in (None, "", False) for k, v in tech_filters.items() if k != "min_value_cr")
    funnel = [{"label": "All NSE stocks", "n": int(len(snap))}]
    if tech_filters.get("min_value_cr"):
        funnel.append({"label": "Liquid enough", "n": int(len(ind.apply(snap, {"min_value_cr": tech_filters["min_value_cr"]})))})
    tech_hit = ind.apply(snap, tech_filters)
    if has_tech:
        funnel.append({"label": "Pass technical filters", "n": int(len(tech_hit))})
    if fund_used:
        have = tech_hit[tech_hit.index.isin(ft.index[~ft["stale"]])]
        funnel.append({"label": "Have checked quarterly results", "n": int(len(have))})
        funnel.append({"label": "Pass fundamental filters", "n": int(len(hit))})
    in_desk = {r["sym"] for r in db.connect().execute("SELECT sym FROM stock WHERE desk=1")}
    cols = ["name", "last", "chg", "value_cr", "stage", "template", "supertrend", "sar", "rs", "rs1m", "rs3m", "rs6m",
            "rs12m", "vs500_55", "vs500_123", "mansfield", "momentum", "high52_pct", "consol_bars", "consol_range", "consol_breakout", "vcp_status", "vcp_n", "vcp_last", "vcp_dist",
            "industry", "sector", "ind_3m", "ind_rank_3m"]
    fcols = ["grade", "sales_yoy", "profit_yoy", "profit_state", "opm_ttm", "pe", "pb", "roce", "gnpa_pct", "nnpa_pct", "pledge_pct", "promo_chg", "insider_net", "latest_q", "is_fin", "checked"]
    rows = []
    for sym, r in hit.head(int(body.get("limit") or 300)).iterrows():
        d = {"sym": sym, "in_desk": sym in in_desk}
        d.update({c: clean(r[c]) for c in cols})
        if sym in ft.index and not ft.loc[sym, "stale"]:
            d.update({c: clean(ft.loc[sym, c]) for c in fcols})
        rows.append(d)
    return {"asof": str(snap["asof"].iloc[0]), "universe": int(len(snap)), "matched": int(len(hit)), "rows": rows,
            "fundamentals": {"used": fund_used, "covered": int((~ft["stale"]).sum())}, "funnel": funnel}


@app.get("/api/screens")
def api_screens():
    """Saved screens with how many stocks match on the latest session and which of them are new since the one before."""
    from . import screens
    return screens.summary(db.connect())


@app.post("/api/screens")
def api_screens_create(body: dict = Body(...)):
    from . import screens
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "give the screen a name")
    sid = screens.create(db.connect(), name, body.get("filters") or {}, body.get("min_value_cr") or 0,
                         market_tables()["snap"], fund_table())
    return {"id": sid}


@app.delete("/api/screens/{sid}")
def api_screens_delete(sid: int):
    from . import screens
    screens.remove(db.connect(), sid)
    return {"ok": True}


@app.post("/api/screens/{sid}/refresh")
def api_screens_refresh(sid: int):
    """Re-run one saved screen on the latest data (the nightly job does this for all of them)."""
    import json as _json
    from . import screens
    con = db.connect()
    r = con.execute("SELECT filters, min_value_cr FROM screen WHERE id=?", (sid,)).fetchone()
    if r is None:
        raise HTTPException(404, "no such screen")
    screens.snapshot(con, sid, _json.loads(r["filters"]), r["min_value_cr"], market_tables()["snap"], fund_table())
    return next(x for x in screens.summary(con) if x["id"] == sid)


@app.get("/screener")
def screener_page():
    return FileResponse(ROOT / "screener.html")


@app.get("/breadth")
def breadth_page():
    return FileResponse(ROOT / "breadth.html")


@app.get("/theme.css")
def theme_css():
    return FileResponse(ROOT / "theme.css", media_type="text/css")


@app.get("/api/status")
def api_status():
    """Outcome of the last nightly run, for the pages to show (and for anyone checking on the job)."""
    f = ROOT / "data" / "nightly_status.json"
    if not f.exists():
        return {"never_run": True}
    s = json.loads(f.read_text(encoding="utf-8"))
    s["age_hours"] = round((__import__("time").time() - f.stat().st_mtime) / 3600, 1)
    return s
