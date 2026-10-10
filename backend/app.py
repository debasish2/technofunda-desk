"""API + dashboard server: uvicorn backend.app:app --reload"""
import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, db, quality

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="TechnoFunda Desk")
app.add_middleware(GZipMiddleware, minimum_size=1200)       # the price history and screener results are large JSON
app.mount("/vendor", StaticFiles(directory=ROOT / "vendor"), name="vendor")      # third-party libraries, kept local


# ---------------------------------------------------------------- login (see backend/auth.py)
OPEN_PATHS = {"/login", "/setup", "/logout", "/api/status", "/favicon.ico"}      # /api/status lets start-desk.bat ask whether the server is up


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not auth.enabled() or path in OPEN_PATHS:
        return await call_next(request)
    user = auth.read_token(request.cookies.get(auth.COOKIE))
    if user:
        request.state.user = user
        return await call_next(request)
    if path.startswith("/api") or path.endswith((".js", ".css", ".json")):
        return JSONResponse({"detail": "login required"}, status_code=401)
    nxt = path + ("?" + request.url.query if request.url.query else "")
    from urllib.parse import quote
    return RedirectResponse("/login?next=" + quote(nxt, safe="/?=&"), status_code=303)


@app.middleware("http")
async def revalidate(request: Request, call_next):
    """Pages, stylesheets and scripts are checked against the server on every load (answered with a tiny 304 when unchanged), so a change shows
    up at once instead of whenever the browser's own guess about freshness runs out."""
    resp = await call_next(request)
    if "cache-control" not in resp.headers and resp.headers.get("content-type", "").startswith(("text/html", "text/css", "application/javascript", "text/javascript")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


_CSS = """:root{--bg:#f4f5f8;--panel:#fff;--ink:#14171f;--muted:#6a7282;--line:#dfe3ea;--accent:#4b5df5}
@media (prefers-color-scheme:dark){:root{--bg:#0d1017;--panel:#161a23;--ink:#eceff6;--muted:#8d95a6;--line:#252b38;--accent:#7b89ff}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
form{width:100%;max-width:360px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:28px 24px;box-shadow:0 10px 40px rgba(0,0,0,.12)}
h1{margin:0 0 4px;font-size:21px}p{margin:0 0 18px;color:var(--muted);font-size:13.5px}label{display:block;margin:12px 0 4px;font-size:13px;color:var(--muted)}
input{width:100%;height:42px;border:1px solid var(--line);border-radius:10px;padding:0 12px;background:var(--bg);color:var(--ink);font-size:15px}input:focus{outline:2px solid var(--accent);border-color:transparent}
button{width:100%;height:44px;margin-top:20px;border:0;border-radius:10px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.err{margin:14px 0 0;padding:9px 12px;border-radius:9px;background:rgba(224,49,49,.12);color:#e03131;font-size:13.5px}"""


def _form(title, sub, fields, button, msg="", nxt="", action="/login"):
    inputs = "".join(f'<label for="{n}">{lab}</label><input id="{n}" name="{n}" type="{t}" autocomplete="{ac}" required{" autofocus" if i == 0 else ""}>'
                     for i, (n, lab, t, ac) in enumerate(fields))
    hidden = f'<input type="hidden" name="next" value="{nxt}">' if nxt else ""
    err = f'<div class="err" role="alert">{msg}</div>' if msg else ""
    return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<meta name="robots" content="noindex"><title>{title}</title><style>{_CSS}</style></head><body>'
                        f'<form method="post" action="{action}"><h1>{title}</h1><p>{sub}</p>{inputs}{hidden}<button type="submit">{button}</button>{err}</form></body></html>',
                        headers={"Cache-Control": "no-store"})


def _safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


async def _fields(request: Request):
    from urllib.parse import parse_qs
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _login_response(request: Request, user, nxt):
    r = RedirectResponse(_safe_next(nxt), status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(auth.COOKIE, auth.make_token(user), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")
    return r


def _addr(request: Request):
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")


@app.get("/login")
def login_page(request: Request, next: str = "/", bad: int = 0, wait: int = 0):
    if not auth.enabled():
        return RedirectResponse("/setup", status_code=303)
    msg = "Too many wrong attempts. Wait a minute and try again." if wait else ("That user name or password is not right." if bad else "")
    return _form("TechnoFunda Desk", "Sign in to continue.", [("username", "User name", "text", "username"), ("password", "Password", "password", "current-password")],
                 "Sign in", msg, _safe_next(next).replace('"', ""))


@app.post("/login")
async def login_submit(request: Request):
    f = await _fields(request)
    nxt = _safe_next(f.get("next", "/"))
    from urllib.parse import quote
    addr = _addr(request)
    if auth.locked(addr):
        return RedirectResponse(f"/login?wait=1&next={quote(nxt, safe='/?=&')}", status_code=303)
    if auth.check(f.get("username", ""), f.get("password", "")):
        auth.succeeded(addr)
        return _login_response(request, f["username"].strip(), nxt)
    auth.failed(addr)
    return RedirectResponse(f"/login?bad=1&next={quote(nxt, safe='/?=&')}", status_code=303)


def _is_local(request: Request):
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1") and "x-forwarded-for" not in request.headers


@app.get("/setup")
def setup_page(request: Request, msg: str = ""):
    if auth.enabled():
        return RedirectResponse("/login", status_code=303)
    if not _is_local(request):
        return PlainTextResponse("The first login has to be created on the PC that runs the server: open http://localhost:8000/setup there.", status_code=403)
    return _form("Create your login", "This PC only. Choose the user name and password you will use on every device.",
                 [("username", "User name", "text", "username"), ("password", "Password (8+ characters)", "password", "new-password"), ("again", "Password again", "password", "new-password")],
                 "Create login", msg.replace("<", ""), action="/setup")


@app.post("/setup")
async def setup_submit(request: Request):
    if auth.enabled() or not _is_local(request):
        return RedirectResponse("/login", status_code=303)
    f = await _fields(request)
    from urllib.parse import quote
    if f.get("password") != f.get("again"):
        return RedirectResponse("/setup?msg=" + quote("The two passwords differ."), status_code=303)
    try:
        name = auth.add_user(f.get("username", ""), f.get("password", ""), admin=True)
    except ValueError as e:
        return RedirectResponse("/setup?msg=" + quote(str(e)), status_code=303)
    db.connect().execute("UPDATE screen SET user=? WHERE user='me'", (name,)).connection.commit()
    return _login_response(request, name, "/")


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(auth.COOKIE, path="/")
    return r


def _who(request: Request):
    """The signed-in user's name; 'me' while the login is off (the name saved screens were filed under before there were logins)."""
    return getattr(request.state, "user", None) or "me"


def _need_user(request: Request):
    u = getattr(request.state, "user", None)
    if not u:
        raise HTTPException(404, "the login is not switched on")
    return u


def _need_admin(request: Request):
    u = _need_user(request)
    if not auth.is_admin(u):
        raise HTTPException(403, "administrators only")
    return u


@app.get("/api/me")
def whoami(request: Request):
    u = getattr(request.state, "user", None)
    return {"auth": auth.enabled(), "user": u, "display": (auth.info(u) or {}).get("display") if u else None}


@app.get("/profile")
def profile_page():
    return FileResponse(ROOT / "profile.html")


@app.get("/prefs-sync.js")
def prefs_sync_js():
    return FileResponse(ROOT / "prefs-sync.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/api/profile")
def api_profile(request: Request):
    u = _need_user(request)
    prefs = auth.load_prefs(u)["items"]
    return {"me": auth.info(u), "users": auth.list_users() if auth.is_admin(u) else None,
            "settings": {"keys": len(prefs), "bytes": sum(len(k) + len(v) for k, v in prefs.items())},
            "screens": len(db.connect().execute("SELECT id FROM screen WHERE user=?", (u,)).fetchall())}


@app.post("/api/profile/display")
def api_profile_display(request: Request, body: dict = Body(...)):
    auth.set_display(_need_user(request), str(body.get("display", "")))
    return {"ok": True}


@app.post("/api/profile/password")
def api_profile_password(request: Request, body: dict = Body(...)):
    u = _need_user(request)
    try:
        auth.change_password(u, str(body.get("current", "")), str(body.get("new", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    r = JSONResponse({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(auth.COOKIE, auth.make_token(u), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")
    return r      # every other device is signed out: the old cookies no longer match the new password


@app.post("/api/users")
def api_users_add(request: Request, body: dict = Body(...)):
    _need_admin(request)
    name = str(body.get("name", "")).strip()
    if auth.exists(name):
        raise HTTPException(400, "that user name is taken")
    try:
        auth.add_user(name, str(body.get("password", "")), admin=bool(body.get("admin")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/users/{name}/password")
def api_users_password(name: str, request: Request, body: dict = Body(...)):
    me = _need_admin(request)
    if not auth.exists(name):
        raise HTTPException(404, "no such user")
    try:
        auth.add_user(name, str(body.get("password", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    r = JSONResponse({"ok": True})
    if name == me:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        r.set_cookie(auth.COOKIE, auth.make_token(me), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")
    return r


@app.delete("/api/users/{name}")
def api_users_remove(name: str, request: Request):
    me = _need_admin(request)
    if name == me:
        raise HTTPException(400, "you cannot remove the account you are using")
    try:
        auth.remove_user(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.connect().execute("DELETE FROM screen_hit WHERE screen_id IN (SELECT id FROM screen WHERE user=?)", (name,)).connection.commit()
    db.connect().execute("DELETE FROM screen WHERE user=?", (name,)).connection.commit()
    return {"ok": True}


@app.get("/api/prefs")
def api_prefs_get(request: Request):
    u = getattr(request.state, "user", None)
    if not u:
        return {"sync": False}
    return {"sync": True, **auth.load_prefs(u)}


@app.put("/api/prefs")
def api_prefs_put(request: Request, body: dict = Body(...)):
    try:
        auth.save_prefs(_need_user(request), body.get("items") or {})
    except ValueError as e:
        raise HTTPException(413, str(e))
    return {"ok": True}


@app.delete("/api/prefs")
def api_prefs_delete(request: Request):
    auth.delete_prefs(_need_user(request))
    return {"ok": True}


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


@app.get("/api/plan/{sym}")
def api_plan(sym: str):
    """The swing playbook's checklist for one stock (selection, relative strength, entry) and the price levels the trade planner starts from."""
    sym = sym.upper()
    snap = market_tables()["snap"]
    if sym not in snap.index:
        raise HTTPException(404, "no price data for this stock")
    r = snap.loc[sym]
    ft = fund_table()
    f = ft.loc[sym] if sym in ft.index and not bool(ft.loc[sym, "stale"]) else None
    sv = lambda row, k: None if row is None or k not in row.index else clean(row[k])
    last, mcap = sv(r, "last"), sv(r, "mcap")
    checks = []

    def add(group, label, value, ok, note=None):
        checks.append({"group": group, "label": label, "value": value, "ok": ok, "note": note})

    nf = lambda v, d=1, suf="": "no data" if v is None else f"{v:,.{d}f}{suf}"
    py, state = sv(f, "profit_yoy"), sv(f, "profit_state")
    add("Selection", "Quarterly profit growth above 25%", "turned profitable" if state == "loss_to_profit" else nf(py, 0, "%"), None if f is None else bool(state == "loss_to_profit" or (py is not None and py > 25)))
    er = sv(f, "eps_rating")
    add("Selection", "EPS rating above 80", nf(er, 0), None if er is None else er > 80, "Our estimate from profit growth: newest quarter, the one before, and three years.")
    basket = None if mcap is None else "Below 500 Cr" if mcap < 500 else "Small basket (500 to 10,000 Cr)" if mcap < 10000 else "Medium basket (10,000 to 20,000 Cr)" if mcap < 20000 else "Large basket (20,000 Cr and above)"
    add("Selection", "Market cap of Rs 500 Cr or more", nf(mcap, 0, " Cr"), None if mcap is None else mcap >= 500, basket)
    add("Selection", "Price above Rs 20", nf(last, 2), None if last is None else last > 20)
    rk, rkmax = sv(r, "ind_rank_3m"), clean(float(snap["ind_rank_3m"].max())) if "ind_rank_3m" in snap.columns else None
    add("Selection", "Industry in the strongest quarter (3-month rank)", None if rk is None or not rkmax else f"#{rk:.0f} of {rkmax:.0f}", None if rk is None or not rkmax else rk <= 0.25 * rkmax,
        "Industry triggers and fresh institutional buying are not measured here: check the Shareholding tab for FII and DII trends.")
    rs = sv(r, "rs")
    add("Relative strength", "RS rating above 80", nf(rs, 0), None if rs is None else rs > 80)
    h52 = sv(r, "high52_pct")
    add("Relative strength", "Within 20% of the 52-week high", None if h52 is None else f"{-h52:.1f}% below", None if h52 is None else h52 >= -20)
    s200 = sv(r, "sma200")
    add("Relative strength", "Above the 200-day average", None if s200 is None else f"200-DMA {s200:,.2f}", None if s200 is None or last is None else last > s200)
    v30 = sv(r, "value30_cr")
    add("Relative strength", "Average traded value above Rs 5 Cr (30 sessions)", nf(v30, 1, " Cr"), None if v30 is None else v30 > 5)
    add("Entry", "Close to its high (within 10%): buy strength, not weakness", None if h52 is None else f"{-h52:.1f}% below", None if h52 is None else h52 >= -10)
    pb = sv(r, "pullback20")
    add("Entry", "Tight pullback under 8% from the 20-session high", None if pb is None else f"{pb:.1f}%", None if pb is None else pb < 8)
    rv5, rv = sv(r, "rvol5"), sv(r, "rvol")
    add("Entry", "Volume spike of 1.5x average or more in the last 5 sessions", None if rv5 is None else f"{rv5:.1f}x (today {rv:.1f}x)" if rv is not None else f"{rv5:.1f}x", None if rv5 is None else rv5 >= 1.5)
    cb, cr, vs = sv(r, "consol_bars"), sv(r, "consol_range"), sv(r, "vcp_status")
    base = bool(sv(r, "consol_active")) or vs in ("forming", "breakout")
    add("Entry", "In a base, flag or pennant", f"{cb:.0f} sessions, range {cr:.0f}%" if sv(r, "consol_active") and cb else (f"VCP {vs}" if vs else "no base now"), base)
    pe = sv(f, "pe")
    add("Entry", "PE under 30, unless growth justifies it", nf(pe, 1, "x"), None if pe is None else pe < 30, None if pe is None or pe < 30 else "Above 30: acceptable only if profit growth is strong enough to justify the multiple.")
    levels = {"last": last, "ema21": sv(r, "ema21"), "low20": sv(r, "low20"), "high20": sv(r, "high20"), "sma50": sv(r, "sma50"), "sma200": s200,
              "base_range": cr if sv(r, "consol_active") else None, "mcap": mcap, "basket": basket, "asof": str(r["asof"]) if "asof" in r.index else None}
    return {"sym": sym, "name": sv(r, "name"), "checks": checks, "levels": levels}


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


@app.get("/api/intraday/{sym}")
def api_intraday(sym: str, iv: str = "5m"):
    """5-minute, 15-minute or hourly bars for the Desk's intraday chart (Yahoo Finance, about 15 minutes late; see backend/intraday.py)."""
    from . import intraday
    try:
        return intraday.bars(sym.upper(), iv)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/live/{sym}")
def api_live(sym: str):
    """Today's session as one daily candle (from Yahoo's 5-minute bars), for the Desk's daily chart to add to the stored history."""
    from . import intraday
    return intraday.session_bar(sym.upper())


@app.get("/api/company/{sym}/announcements")
def api_company_announcements(sym: str, refresh: int = 0):
    """BSE announcements sorted by kind (concalls, credit ratings, everything else) for the Documents sub-tab (see backend/announcements.py)."""
    from . import announcements
    return announcements.get(sym.upper(), refresh=bool(refresh))


@app.get("/api/company/{sym}/ia/{stat}")
def api_company_ia(sym: str, stat: str):
    """A statement downloaded from IndianAPI (balancesheet, cashflow, ratios, shareholding...), as sent: {body, fetched}, or {} when we hold none."""
    import json as _json
    from . import indianapi
    if stat not in indianapi.ALL_KEYS:
        raise HTTPException(404, "unknown statement")
    r = indianapi._con().execute("SELECT body, fetched FROM main.ia_stat WHERE sym=? AND stat=?", (sym.upper(), stat)).fetchone()
    return {"body": _json.loads(r["body"]), "fetched": r["fetched"]} if r else {}


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


@app.get("/events")
def events_page():
    return FileResponse(ROOT / "events.html")


@app.get("/api/events/ipos")
def api_events_ipos(refresh: int = 0):
    """IPOs: open, upcoming and the last 500 days of closed and listed issues, from NSE (see backend/feeds.py)."""
    from . import feeds
    return feeds.ipos(refresh=bool(refresh))


@app.get("/api/events/calendar")
def api_events_calendar(refresh: int = 0):
    """Board meetings announced to NSE (results, dividends, fund raising), by date."""
    from . import feeds
    return feeds.calendar(refresh=bool(refresh))


@app.get("/api/company/{sym}/analysts")
def api_company_analysts(sym: str, refresh: int = 0):
    from . import feeds
    return feeds.analysts(sym.upper(), refresh=bool(refresh))


@app.get("/api/company/{sym}/news")
def api_company_news(sym: str, refresh: int = 0):
    from . import feeds
    return feeds.news(sym.upper(), refresh=bool(refresh))


@app.get("/api/company/{sym}/actions")
def api_company_actions(sym: str, refresh: int = 0):
    from . import feeds
    return feeds.actions(sym.upper(), refresh=bool(refresh))


@app.get("/funds")
def funds_page():
    return FileResponse(ROOT / "funds.html")


FUND_SORTS = {"r1w", "r1m", "r3m", "r6m", "r1y", "r3y", "r5y", "r10y", "vol3y", "dd3y", "sharpe3y", "nav", "name"}


@app.get("/api/funds/meta")
def api_funds_meta():
    """Categories and fund houses with at least one fund that has returns worked out, and the date of the newest NAV (see backend/funds.py)."""
    from . import funds
    con = funds.connect()
    cats = [{"name": r[0], "n": r[1]} for r in con.execute("SELECT s.category, COUNT(*) FROM scheme s JOIN metric m ON m.code=s.code WHERE s.keep=1 GROUP BY s.category ORDER BY 2 DESC")]
    amcs = [{"name": r[0], "n": r[1]} for r in con.execute("SELECT s.amc, COUNT(*) FROM scheme s JOIN metric m ON m.code=s.code WHERE s.keep=1 GROUP BY s.amc ORDER BY s.amc")]
    return {"categories": cats, "amcs": amcs, "asof": con.execute("SELECT MAX(nav_date) FROM scheme").fetchone()[0], "funds": sum(c["n"] for c in cats)}


@app.get("/api/funds")
def api_funds(q: str = "", cat: str = "", amc: str = "", plan: str = "", sort: str = "r1y", dir: int = -1, limit: int = 400):
    """Funds with their returns and risk figures, filtered and sorted. Funds without enough history for the sort column go last."""
    from . import funds
    if sort not in FUND_SORTS:
        raise HTTPException(400, "unknown sort column")
    where, args = ["s.keep=1"], []
    for term in q.split():
        where.append("s.name LIKE ?")
        args.append(f"%{term}%")
    if cat:
        where.append("s.category=?")
        args.append(cat)
    if amc:
        where.append("s.amc=?")
        args.append(amc)
    if plan in ("Direct", "Regular"):
        where.append("s.plan LIKE ?")
        args.append(plan + "%")
    col = "s.name" if sort == "name" else "s.nav" if sort == "nav" else "m." + sort
    order = f"{col} IS NULL, {col} {'DESC' if dir < 0 else 'ASC'}"
    sql = (f"SELECT s.code, s.name, s.amc, s.category, s.plan, s.nav, s.nav_date, m.r1w, m.r1m, m.r3m, m.r6m, m.r1y, m.r3y, m.r5y, m.r10y, m.vol3y, m.dd3y, m.sharpe3y, m.first_d "
           f"FROM scheme s JOIN metric m ON m.code=s.code WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?")
    con = funds.connect()
    rows = [dict(r) for r in con.execute(sql, args + [max(1, min(limit, 1000))])]
    total = con.execute(f"SELECT COUNT(*) FROM scheme s JOIN metric m ON m.code=s.code WHERE {' AND '.join(where)}", args).fetchone()[0]
    return {"rows": rows, "total": total}


@app.get("/api/funds/{code}")
def api_fund(code: str):
    """One fund: its details, returns and risk figures, and the whole NAV history."""
    from . import funds
    con = funds.connect()
    s = con.execute("SELECT * FROM scheme WHERE code=?", (code,)).fetchone()
    if s is None:
        raise HTTPException(404, "no such fund")
    m = con.execute("SELECT * FROM metric WHERE code=?", (code,)).fetchone()
    nav = [[r[0], r[1]] for r in con.execute("SELECT d, v FROM nav WHERE code=? ORDER BY d", (code,))]
    return {"scheme": dict(s), "metric": dict(m) if m else None, "nav": nav}


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
            "industry", "sector", "ind_3m", "ind_rank_3m", "mcap", "rsi14", "vs500_252", "pullback20", "rvol5", "listed_days", "value30_cr",
            "adx14", "adx14w", "rsi14w", "vol5", "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_1y", "ret_3y", "low52_pct", "hi52", "lo52", "d50", "d200", "rs1m", "rs3m", "rs6m", "rs12m"]
    fcols = ["grade", "sales_yoy", "profit_yoy", "profit_state", "opm_ttm", "pe", "pb", "roce", "gnpa_pct", "nnpa_pct", "pledge_pct", "promo_chg", "insider_net", "latest_q", "is_fin", "checked", "profit_cagr3", "profit_cagr_fy", "de", "eps_rating"]
    rows = []
    for sym, r in hit.head(int(body.get("limit") or 300)).iterrows():
        d = {"sym": sym, "in_desk": sym in in_desk}
        d.update({c: clean(r[c]) for c in cols})
        if sym in ft.index and not ft.loc[sym, "stale"]:
            d.update({c: clean(ft.loc[sym, c]) for c in fcols})
        rows.append(d)
    return {"asof": str(snap["asof"].iloc[0]), "universe": int(len(snap)), "matched": int(len(hit)), "rows": rows,
            "fundamentals": {"used": fund_used, "covered": int((~ft["stale"]).sum())}, "funnel": funnel}


@app.get("/api/screener/cols")
def api_cols():
    """Statement, valuation, holding and classification columns for every stock (the Screener's Growth, Profit/Loss, Balance Sheet... tabs)."""
    from . import screencols
    df = screencols.table(market_tables()["snap"])
    cols = list(df.columns)
    return {"cols": cols, "data": {sym: [clean(v) for v in row] for sym, row in zip(df.index, df.itertuples(index=False, name=None))}}


@app.post("/api/screener/bars")
def api_bars(body: dict = Body(...)):
    """Daily bars for a handful of stocks (the Screener's Chart tab): {"syms": [...], "bars": 130} -> {"d": [dates], "s": {sym: {o,h,l,c,v}}}."""
    wide = market_tables()["data"][0]
    syms = [x for x in (body.get("syms") or [])[:60] if x in wide["c"].columns]
    n = max(5, min(int(body.get("bars") or 130), 800))
    idx = wide["c"].index[-n:]
    out = {}
    for sym in syms:
        out[sym] = {k: [None if v != v else round(float(v), 2 if k != "v" else 0) for v in wide[k].loc[idx, sym].tolist()] for k in ("o", "h", "l", "c", "v")}
    return {"d": [str(x) for x in idx], "s": out}


@app.get("/api/screener/indexes")
def api_index_list():
    from . import feeds
    return feeds.INDEXES


@app.get("/api/screener/index")
def api_index(name: str):
    from . import feeds
    if name not in feeds.INDEXES:
        raise HTTPException(404, "unknown index")
    return feeds.index_members(name)


@app.get("/api/screener/results-due")
def api_results_due():
    """symbol -> date of the next board meeting for financial results, from the NSE event calendar."""
    from . import feeds
    c = feeds.calendar()
    if not c.get("ok", True):
        return c
    out = {}
    for r in c.get("rows", []):
        if r.get("symbol") and "result" in (r.get("purpose") or "").lower() and r["symbol"] not in out:
            out[r["symbol"]] = r["date"]
    return {"ok": True, "due": out, "asof": c.get("asof")}


@app.get("/api/screens")
def api_screens(request: Request):
    """Saved screens with how many stocks match on the latest session and which of them are new since the one before."""
    from . import screens
    return screens.summary(db.connect(), _who(request))


@app.post("/api/screens")
def api_screens_create(request: Request, body: dict = Body(...)):
    from . import screens
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "give the screen a name")
    sid = screens.create(db.connect(), name, body.get("filters") or {}, body.get("min_value_cr") or 0,
                         market_tables()["snap"], fund_table(), user=_who(request))
    return {"id": sid}


@app.delete("/api/screens/{sid}")
def api_screens_delete(sid: int, request: Request):
    from . import screens
    screens.remove(db.connect(), sid, _who(request))
    return {"ok": True}


@app.post("/api/screens/{sid}/refresh")
def api_screens_refresh(sid: int, request: Request):
    """Re-run one saved screen on the latest data (the nightly job does this for all of them)."""
    import json as _json
    from . import screens
    con = db.connect()
    r = con.execute("SELECT filters, min_value_cr FROM screen WHERE id=? AND user=?", (sid, _who(request))).fetchone()
    if r is None:
        raise HTTPException(404, "no such screen")
    screens.snapshot(con, sid, _json.loads(r["filters"]), r["min_value_cr"], market_tables()["snap"], fund_table())
    return next(x for x in screens.summary(con, _who(request)) if x["id"] == sid)


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
