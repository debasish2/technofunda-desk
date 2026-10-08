"""Industry tracker: how each industry group's stocks have done over a day, a week ... a year.

Groups come from NSE's four-tier structure (macro-economic sector > sector > industry > basic industry, see classify.py) plus
"themes", which are your own groupings defined in data/themes.json. A group's return is the average (or median, or market-cap
weighted average) of its stocks' price returns, taken from the same adjusted daily bars the screener uses; when a live snapshot
exists (backend.live) the newest price is the running price of the day.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import market

THEMES = Path(__file__).resolve().parent.parent / "data" / "themes.json"
RANGES = {"today": 1, "1w": 5, "1m": 21, "3m": 63, "6m": 126, "ytd": None, "1y": 252}
LEVELS = {"macro": "Macro-economic sector", "sector": "Sector", "industry": "Industry", "basic": "Basic industry", "theme": "Theme"}


def classes():
    """sym -> macro, sector, industry, basic, mcap (Rs crore)."""
    con = market.connect()
    df = pd.read_sql("SELECT c.sym, c.macro, c.sector, c.industry, c.basic, c.mcap, u.name FROM class c LEFT JOIN universe u ON u.sym=c.sym WHERE c.basic IS NOT NULL", con)
    return df.set_index("sym")


def load_themes():
    try:
        return json.loads(THEMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def theme_members(cl):
    """theme name -> set of symbols. A theme is {"macro"|"sector"|"industry"|"basic": [names], "symbols": [...], "exclude": [...]}."""
    out = {}
    for name, rule in load_themes().items():
        syms = set(rule.get("symbols", []))
        for lvl in ("macro", "sector", "industry", "basic"):
            if rule.get(lvl):
                syms |= set(cl.index[cl[lvl].isin(rule[lvl])])
        syms -= set(rule.get("exclude", []))
        out[name] = syms & set(cl.index)
    return out


def stock_returns(data):
    """DataFrame (sym x range) of % returns to the newest bar, plus the 50-day-average flag."""
    C = data[0]["c"]
    last = C.iloc[-1]
    ok = last.notna() & (last >= 1)
    out = {}
    for name, k in RANGES.items():
        if name == "ytd":
            prev = [i for i, d in enumerate(C.index) if str(d)[:4] < str(C.index[-1])[:4]]
            base = C.iloc[prev[-1]] if prev else None
        else:
            base = C.iloc[-1 - k] if len(C) > k else None
        out[name] = (last / base - 1) * 100 if base is not None else pd.Series(np.nan, index=C.columns)
    df = pd.DataFrame(out).where(ok, np.nan).replace([np.inf, -np.inf], np.nan)
    df = df.clip(lower=-90, upper=500)                         # a split or a wild small-cap should not own a group
    df["above50"] = (C.iloc[-1] > C.rolling(50, min_periods=50).mean().iloc[-1]).where(ok & (C.count() >= 50))
    df["asof"] = str(C.index[-1])
    return df


def _agg(g, col, agg):
    s = g[col].dropna()
    if s.empty:
        return np.nan
    if agg == "median":
        return float(s.median())
    if agg == "mcap":
        w = g.loc[s.index, "mcap"].fillna(0)
        return float((s * w).sum() / w.sum()) if w.sum() > 0 else float(s.mean())
    return float(s.mean())


def groups(data, level, min_mcap=500, min_n=3, agg="mean", rs=None):
    """One row per group: stocks counted, a return for every range, share of stocks above their 50-day average, average RS rating."""
    cl = classes()
    ret = stock_returns(data).drop(columns="asof")
    df = cl.join(ret, how="inner")
    df = df[df["mcap"].fillna(0) >= min_mcap]
    if rs is not None:
        df["rs"] = rs.reindex(df.index)
    if level == "theme":
        members = theme_members(cl)
        parts = [(name, df.loc[df.index.intersection(list(s))]) for name, s in members.items()]
    else:
        parts = list(df.groupby(level))
    rows = []
    for name, g in parts:
        if not name or len(g) < min_n:
            continue
        r = {"name": name, "n": int(len(g)), "mcap": float(g["mcap"].sum()),
             "above50": float(g["above50"].astype(float).mean() * 100) if g["above50"].notna().any() else None,
             "rs": float(g["rs"].mean()) if "rs" in g and g["rs"].notna().any() else None}
        for k in RANGES:
            v = _agg(g, k, agg)
            r[k] = None if v != v else round(v, 2)
        rows.append(r)
    return rows


def members(data, level, name, agg="mean", rs=None, min_mcap=0):
    cl = classes()
    ret = stock_returns(data).drop(columns="asof")
    df = cl.join(ret, how="inner")
    if level == "theme":
        df = df.loc[df.index.intersection(list(theme_members(cl).get(name, set())))]
    else:
        df = df[df[level] == name]
    df = df[df["mcap"].fillna(0) >= min_mcap]
    if rs is not None:
        df["rs"] = rs.reindex(df.index)
    out = []
    for sym, r in df.iterrows():
        out.append({"sym": sym, "name": r["name"], "mcap": None if r["mcap"] != r["mcap"] else round(float(r["mcap"]), 0),
                    "basic": r["basic"], **{k: (None if r[k] != r[k] else round(float(r[k]), 2)) for k in RANGES},
                    "rs": None if "rs" not in r or r["rs"] != r["rs"] else float(r["rs"])})
    return out


# ---------------------------------------------------------------- screener and Desk integration
def token():
    """Changes whenever the classification or market caps change (so cached tables know to rebuild)."""
    r = market.connect().execute("SELECT COUNT(*), MAX(updated), ROUND(SUM(mcap)) FROM class").fetchone()
    return tuple(r)


def enrich(snap, data):
    """The snapshot plus each stock's four industry names and how its industry (NSE 'industry' tier) has done:
    ind_1w / ind_1m / ind_3m returns and ind_rank_1m / ind_rank_3m (1 = best of ind_groups ranked industries)."""
    base = snap.drop(columns=[c for c in snap.columns if c in ("macro", "sector", "industry", "basic", "mcap") or c.startswith("ind_")])
    try:
        cl = classes()
        rows = pd.DataFrame(groups(data, "industry", 500, 3, "mean", base["rs"] if "rs" in base else None))
    except Exception:
        return base
    if rows.empty:
        return base
    rows = rows.set_index("name")
    rows["ind_rank_1m"] = rows["1m"].rank(ascending=False, method="min")
    rows["ind_rank_3m"] = rows["3m"].rank(ascending=False, method="min")
    per = pd.DataFrame({"ind_1w": cl["industry"].map(rows["1w"]), "ind_1m": cl["industry"].map(rows["1m"]), "ind_3m": cl["industry"].map(rows["3m"]),
                        "ind_rank_1m": cl["industry"].map(rows["ind_rank_1m"]), "ind_rank_3m": cl["industry"].map(rows["ind_rank_3m"])}, index=cl.index)
    per["ind_groups"] = float(len(rows))
    out = base.join(cl[["macro", "sector", "industry", "basic", "mcap"]], how="left").join(per, how="left")
    try:                                                    # BSE's market cap is missing for some stocks: price times share count fills it in
        from . import db
        sh = pd.Series({r["sym"]: r["shares_cr"] for r in db.connect().execute("SELECT sym, shares_cr FROM stock WHERE shares_cr > 0")})
        out["mcap"] = out["mcap"].fillna(out["last"] * sh.reindex(out.index))
    except Exception:
        pass
    return out


def fields():
    """Screener filters for the Industry group; choice lists come from the classification table."""
    cl = classes()
    opt = lambda col: {v: v for v in sorted(cl[col].dropna().unique())}
    g = "Industry"
    return [
        {"group": g, "id": "macro", "label": "Macro-economic sector is", "type": "choice", "options": opt("macro")},
        {"group": g, "id": "sector", "label": "Sector is", "type": "choice", "options": opt("sector")},
        {"group": g, "id": "industry", "label": "Industry is", "type": "choice", "options": opt("industry")},
        {"group": g, "id": "basic", "label": "Basic industry is", "type": "choice", "options": opt("basic")},
        {"group": g, "id": "ind_top3m", "label": "Industry in the top N by 3-month return", "type": "max", "unit": "rank",
         "help": "Industries (the NSE 'industry' tier, ₹500 Cr+ stocks, 3+ per group) ranked by average 3-month return; 1 is the best. The same numbers as the Industries page."},
        {"group": g, "id": "ind_top1m", "label": "Industry in the top N by 1-month return", "type": "max", "unit": "rank"},
        {"group": g, "id": "ind_3m", "label": "Industry 3-month return >", "type": "min", "unit": "%"},
        {"group": g, "id": "ind_1m", "label": "Industry 1-month return >", "type": "min", "unit": "%"},
    ]
