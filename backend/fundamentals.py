"""Fundamental metrics per stock, for the screener's fundamental filters.

A line-for-line port of the analysis in setup-desk.html (grades, growth, margins, momentum, cycle stage,
PE / ROCE / ROE), so a stock reads the same on the Desk and in the screener. Year-ago comparisons are
matched by date, never by "four rows back", because quarters can be missing.

Only stocks with checked quarterly results appear here (the Desk's stocks). A stock whose newest result is
more than ~200 days older than its price data is marked stale and excluded from filtering.
"""
from datetime import date, timedelta

import pandas as pd

from . import db, market, quality


def month_end(end, m):
    """Month-end `m` months before `end` (negative = later), as an ISO date."""
    y, mo = int(end[:4]), int(end[5:7])
    i = y * 12 + (mo - 1) - m
    y, mo = divmod(i, 12)                                  # mo is 0-based
    first_of_next = date(y + (mo == 11), (mo + 1) % 12 + 1, 1)
    return (first_of_next - timedelta(days=1)).isoformat()


def grade_of(q, y):
    s = q["sales"] / y["sales"] - 1
    n, p = q["np"], y["np"]
    if p <= 0 and n > 0:
        return "Turnaround"
    if n <= 0:
        return "Trash"
    e = n / p - 1
    if e >= 1 and s >= 0.25:
        return "Blockbuster"
    if e >= 0.2 and s >= 0.1:
        return "Good"
    if e >= 0:
        return "OK"
    return "Trash"


def analyse(quarters, last_price, last_bar_date, shares_cr, cap_employed, equity, kind="corp"):
    """quarters: list of dicts (end, sales, op, np), any order. Returns a metrics dict, or None if unusable."""
    Q = sorted(quarters, key=lambda q: q["end"])
    if len(Q) < 5:
        return None
    by = {q["end"]: q for q in Q}
    ya = lambda q: by.get(month_end(q["end"], 12)) if q else None
    graded = [(q, ya(q)) for q in Q if ya(q) and q["sales"] > 0 and ya(q)["sales"] > 0]
    if not graded:
        return None
    grades = [grade_of(q, y) for q, y in graded]
    LQ, YQ = graded[-1]
    opy = lambda q: (q["op"] / ya(q)["op"] - 1) if q and ya(q) and ya(q)["op"] > 0 else None
    y3 = [opy(by.get(month_end(LQ["end"], 6))), opy(by.get(month_end(LQ["end"], 3))), opy(LQ)]
    if any(v is None for v in y3):
        momentum = "Steady"
    elif y3[2] > y3[1] and y3[2] > y3[0] + 0.05:
        momentum = "Accelerating"
    elif y3[2] < y3[1] and y3[2] < y3[0] - 0.05:
        momentum = "Decelerating"
    else:
        momentum = "Steady"
    lg, recent = grades[-1], grades[-4:]
    opm_now, opm_prev = LQ["op"] / LQ["sales"] * 100, YQ["op"] / YQ["sales"] * 100
    if lg == "Trash":
        cycle = "Declining"
    elif "Turnaround" in grades[-2:] or ("Trash" in recent and lg in ("Good", "Blockbuster")):
        cycle = "Early"
    elif momentum == "Accelerating" and opm_now > opm_prev:
        cycle = "Mid"
    elif momentum == "Decelerating":
        cycle = "Late"
    else:
        cycle = "Mature"
    # trailing twelve months: the last four quarters, annualised if one is missing (needs at least three)
    W = [q for q in Q if month_end(LQ["end"], 12) < q["end"] <= LQ["end"]]
    if len(W) < 3 or sum(q["sales"] for q in W) <= 0:
        return None
    oneoff = any(q["sales"] > 0 and q["np"] > q["sales"] for q in W)
    k = 4 / len(W)
    ttm = lambda f: sum(q[f] for q in W) * k
    ttm_np, ttm_op, ttm_s = ttm("np"), ttm("op"), ttm("sales")
    mcap = last_price * shares_cr if shares_cr else None
    fin = kind == "fin"                       # banks / lenders / insurers: no margin, no ROCE (see financials.py)
    stale = (date.fromisoformat(last_bar_date) - date.fromisoformat(LQ["end"])).days > 200
    return {
        "latest_q": LQ["end"], "stale": stale, "grade": lg,
        "sales_yoy": (LQ["sales"] / YQ["sales"] - 1) * 100,
        # growth off a loss has no meaningful percentage, so it is None with an explicit state (no magic numbers:
        # a genuine +1,870% must stay a number)
        "profit_yoy": (LQ["np"] / YQ["np"] - 1) * 100 if YQ["np"] > 0 else None,
        "profit_state": ("loss_to_profit" if LQ["np"] > 0 else "loss_both") if YQ["np"] <= 0 else None,
        "opm_ttm": None if fin else ttm_op / ttm_s * 100, "opm_now": None if fin else opm_now,
        "opm_prev": None if fin else opm_prev, "opm_expanding": False if fin else opm_now > opm_prev,
        "momentum": momentum, "cycle": cycle,
        # No PE or ROE when any of the last four quarters earned more profit than it had sales: that is a one-off gain
        # (demerger, asset sale, tax write-back), and it would make the stock look absurdly cheap in a value screen
        "mcap_cr": mcap, "pe": (mcap / ttm_np) if (mcap and ttm_np > 0 and not oneoff) else None,
        "roce": None if fin else ((ttm_op * 0.8 / cap_employed * 100) if cap_employed and cap_employed > 0 else None),
        "pb": (mcap / equity) if (mcap and equity and equity > 0) else None, "is_fin": fin,
        "roe": (ttm_np / equity * 100) if equity and equity > 0 and not oneoff else None,
        "oneoff": oneoff,
        "profitable": ttm_np > 0,
    }


def snapshot(con=None):
    """One row per Desk stock with usable results."""
    con = con or db.connect()
    rows = {}
    # newest close per stock, from the market database (every stock is there; screener-only stocks have no Desk bars)
    mcon = market.connect()
    last = {r[0]: (r[1], r[2]) for r in mcon.execute(
        "SELECT sym, d, c FROM mbar WHERE (sym, d) IN (SELECT sym, MAX(d) FROM mbar GROUP BY sym)")}
    for s in con.execute("SELECT * FROM stock"):
        bar = {"d": last[s["sym"]][0], "c": last[s["sym"]][1]} if s["sym"] in last else None
        if not bar:
            continue
        qs = [dict(end=r["qend"], sales=r["sales"], op=r["op"], np=r["np"]) for r in con.execute(
            "SELECT qend, sales, op, np FROM quarter WHERE sym=? AND flags='' AND sales IS NOT NULL AND op IS NOT NULL "
            "AND np IS NOT NULL ORDER BY qend", (s["sym"],))]
        try:
            m = analyse(qs, bar["c"], bar["d"], s["shares_cr"], s["cap_employed"], s["equity"], s["kind"] or "corp")
        except (ZeroDivisionError, ValueError, TypeError, KeyError):
            m = None
        if m:
            m["sector"] = s["sector"]
            m["price_date"] = bar["d"]
            rows[s["sym"]] = m
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "sym"
    # asset quality (banks only): the newest reported ratios, ignored if older than ~200 days
    npa = {r["sym"]: r for r in con.execute(
        "SELECT sym, qend, gnpa_pct, nnpa_pct FROM npa WHERE (sym, qend) IN (SELECT sym, MAX(qend) FROM npa GROUP BY sym)")}

    def fresh(sym, col):
        r = npa.get(sym)
        if r is None or r[col] is None or sym not in df.index:
            return None
        return r[col] if (date.fromisoformat(df.loc[sym, "price_date"]) - date.fromisoformat(r["qend"])).days <= 200 else None

    nq = quality.newest(con)
    df["checked"] = [nq[s][1] if s in nq and nq[s][0] == df.loc[s, "latest_q"] else None for s in df.index]     # f / m / u for the newest quarter
    df["gnpa_pct"] = [fresh(s, "gnpa_pct") for s in df.index]
    df["nnpa_pct"] = [fresh(s, "nnpa_pct") for s in df.index]
    # promoter pledge / holding trend / insider activity, for stocks whose NSE disclosures have been loaded
    loaded = {r["sym"] for r in con.execute("SELECT sym FROM disc_fetch")}
    plg = {r["sym"]: r["pledged_pct_of_promoter"] for r in con.execute(
        "SELECT sym, pledged_pct_of_promoter FROM pledge WHERE (sym, asof) IN (SELECT sym, MAX(asof) FROM pledge GROUP BY sym)")}
    hold = {}
    for r in con.execute("SELECT sym, qend, promoter_pct FROM holding ORDER BY sym, qend DESC"):
        hold.setdefault(r["sym"], []).append(r["promoter_pct"])
    since = (date.today() - timedelta(days=365)).isoformat()
    net = {r["sym"]: (r["buy"] or 0) - (r["sell"] or 0) for r in con.execute(
        "SELECT sym, SUM(CASE WHEN kind='Buy' THEN val_cr END) AS buy, SUM(CASE WHEN kind='Sell' THEN val_cr END) AS sell "
        "FROM insider WHERE d >= ? GROUP BY sym", (since,))}
    df["pledge_pct"] = [plg.get(s) for s in df.index]
    df["promo_pct"] = [hold[s][0] if s in hold else None for s in df.index]
    df["promo_chg"] = [round(hold[s][0] - hold[s][4], 2) if s in hold and len(hold[s]) > 4 else None for s in df.index]
    df["insider_net"] = [round(net.get(s, 0.0), 2) if s in loaded else None for s in df.index]     # fetched but quiet = 0, not unknown
    return df


# ---------------------------------------------------------------- screener fields
FIELDS = [
    {"group": "Fundamentals", "id": "kind", "label": "Company type", "type": "choice", "fundamental": True,
     "options": {"fin": "Banks, lenders & insurers only", "corp": "Exclude banks, lenders & insurers"},
     "help": "Banks and lenders are graded on net interest income, have no operating margin or ROCE, and are best judged on P/B and ROE."},
    {"group": "Fundamentals", "id": "grade", "label": "Latest result", "type": "choice", "fundamental": True,
     "options": {"blockbuster": "Blockbuster", "good_plus": "Good or better", "ok_plus": "OK or better",
                 "turnaround": "Turnaround", "not_trash": "Not a Trash result"},
     "help": "Grade of the newest quarter against the same quarter a year earlier. Blockbuster: profit at least doubled "
             "and sales up 25%+. Good: profit +20% and sales +10%. OK: profit not lower. Turnaround: a loss became a profit."},
    {"group": "Fundamentals", "id": "sales_yoy", "label": "Sales growth YoY >", "type": "min", "unit": "%", "fundamental": True,
     "help": "Newest quarter's sales against the same quarter a year earlier. For banks and lenders this is net interest income; "
             "for insurers, total revenue."},
    {"group": "Fundamentals", "id": "profit_yoy", "label": "Profit growth YoY >", "type": "min", "unit": "%", "fundamental": True,
     "help": "Net profit, newest quarter against a year earlier. A loss that became a profit has no percentage and passes any threshold."},
    {"group": "Fundamentals", "id": "opm_ttm", "label": "Operating margin (12 mo) >", "type": "min", "unit": "%", "fundamental": True},
    {"group": "Fundamentals", "id": "opm_expanding", "label": "Operating margin expanding YoY", "type": "flag", "fundamental": True},
    {"group": "Fundamentals", "id": "earn_momentum", "label": "Earnings momentum", "type": "choice", "fundamental": True,
     "options": {"Accelerating": "Accelerating", "Steady": "Steady", "Decelerating": "Decelerating"},
     "help": "Direction of year-on-year operating-profit growth over the last three quarters."},
    {"group": "Fundamentals", "id": "cycle", "label": "Growth-cycle stage", "type": "choice", "fundamental": True,
     "options": {"Early": "Early", "Mid": "Mid", "Late": "Late", "Mature": "Mature", "Declining": "Declining"}},
    {"group": "Fundamentals", "id": "checked", "label": "Newest result checked against the filing", "type": "flag", "fundamental": True,
     "help": "Leaves out stocks whose newest quarter is Yahoo Finance's figure that no company filing has confirmed yet. Those show a dagger in the Result column."},
    {"group": "Fundamentals", "id": "profitable", "label": "Profitable over the last 12 months", "type": "flag", "fundamental": True},
    {"group": "Fundamentals", "id": "pe", "label": "PE <", "type": "max", "unit": "x", "fundamental": True,
     "help": "Market cap over the last 12 months' net profit. Loss-makers have no PE and never pass this filter."},
    {"group": "Fundamentals", "id": "roce", "label": "ROCE >", "type": "min", "unit": "%", "fundamental": True,
     "help": "0.8 x 12-month operating profit over capital employed (total assets less current liabilities)."},
    {"group": "Fundamentals", "id": "pb", "label": "Price / book <", "type": "max", "unit": "x", "fundamental": True,
     "help": "Market cap over shareholders' equity. Most useful for banks and lenders."},
    {"group": "Fundamentals", "id": "gnpa_pct", "label": "Gross NPA % <", "type": "max", "unit": "%", "fundamental": True,
     "help": "Gross non-performing assets as a share of gross advances, newest quarter. Loaded for the larger banks only."},
    {"group": "Fundamentals", "id": "nnpa_pct", "label": "Net NPA % <", "type": "max", "unit": "%", "fundamental": True},
    {"group": "Promoters & insiders", "id": "pledge_pct", "label": "Promoter pledge % of their shares <", "type": "max", "unit": "%",
     "fundamental": True, "help": "Share of the promoters' own holding that is pledged to lenders, latest quarter. Under 5% is low; above 25% is a warning sign."},
    {"group": "Promoters & insiders", "id": "promo_chg", "label": "Promoter holding change over a year >", "type": "min", "unit": "pts", "fundamental": True,
     "help": "Percentage-point change in promoter holding against four quarters ago. Use 0 to find promoters who are not selling down."},
    {"group": "Promoters & insiders", "id": "insider_net", "label": "Insider net buying, 12 months >", "type": "min", "unit": "₹ Cr", "fundamental": True,
     "help": "Open-market purchases minus open-market sales by insiders in the last 12 months. Off-market transfers, ESOP, gifts and pledges are not counted. Use 0 to find net buyers."},
    {"group": "Fundamentals", "id": "roe", "label": "ROE >", "type": "min", "unit": "%", "fundamental": True},
]
FUND_IDS = {f["id"] for f in FIELDS}


def apply(fund, filters):
    """Rows of `fund` passing every fundamental filter in `filters` (stale results never pass)."""
    m = ~fund["stale"]
    for fid, v in filters.items():
        if fid not in FUND_IDS or v in (None, "", False):
            continue
        if fid == "grade":
            g = fund["grade"]
            m &= {"blockbuster": g == "Blockbuster", "good_plus": g.isin(["Blockbuster", "Good"]),
                  "ok_plus": g.isin(["Blockbuster", "Good", "OK"]), "turnaround": g == "Turnaround",
                  "not_trash": g != "Trash"}[v]
        elif fid == "kind":
            m &= fund["is_fin"].astype(bool) if v == "fin" else ~fund["is_fin"].astype(bool)
        elif fid == "earn_momentum":
            m &= fund["momentum"] == v
        elif fid == "cycle":
            m &= fund["cycle"] == v
        elif fid in ("opm_expanding", "profitable"):
            m &= fund[fid].astype(bool)
        elif fid == "checked":
            m &= fund["checked"].isin(["f", "m"])
        elif fid == "profit_yoy":                              # a loss turning into a profit passes any growth bar
            m &= (fund["profit_yoy"].notna() & (fund["profit_yoy"] > float(v))) | (fund["profit_state"] == "loss_to_profit")
        elif fid in ("pb", "gnpa_pct", "nnpa_pct", "pledge_pct"):
            m &= fund[fid].notna() & (fund[fid] < float(v))
        elif fid == "pe":
            m &= fund["pe"].notna() & (fund["pe"] < float(v))
        else:                                                   # sales_yoy, profit_yoy, opm_ttm, roce, roe: ">" a number
            m &= fund[fid].notna() & (fund[fid] > float(v))
    return fund[m]
