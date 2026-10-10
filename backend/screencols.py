"""Columns for the Screener's tabs (Growth, Profit/Loss, Balance Sheet, Cash Flow, Ratios, Holdings, Valuation) for every stock.

Built from the IndianAPI statements kept in data/ia.db (about 2,400 stocks, 12 years), the latest price and market cap from the snapshot,
and the NSE listing series. Latest financial year = the newest "Mon YYYY" column; TTM = the trailing-twelve-month column.
"""
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from . import market

IA = Path(__file__).resolve().parent.parent / "data" / "ia.db"
_cache = {"key": None, "df": None}
MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def _period(k):
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{4})", k.strip())
    return (int(m.group(2)), MON.get(m.group(1), 0)) if m else None


def _pairs(d):
    """A statement row as chronological (period, value) pairs, TTM excluded."""
    out = [(_period(k), v) for k, v in d.items() if _period(k) and isinstance(v, (int, float))]
    return sorted(out)


def _last(rows, label, *alts):
    for lb in (label, *alts):
        d = rows.get(lb)
        if isinstance(d, dict):
            p = _pairs(d)
            if p:
                return p[-1][1], (p[-2][1] if len(p) > 1 else None), p[-1][0]
    return None, None, None


def _ttm(rows, label, *alts):
    for lb in (label, *alts):
        d = rows.get(lb)
        if isinstance(d, dict) and isinstance(d.get("TTM"), (int, float)):
            return d["TTM"]
    return None


def _pct(s):
    try:
        return float(str(s).replace("%", "").strip())
    except ValueError:
        return None


def _growth(a, b):
    return (a / b - 1) * 100 if a is not None and b not in (None, 0) and b > 0 else None


def _one(stats):
    o = {}
    y, bs, cf, rt, sh, ps = (stats.get(k) or {} for k in ("yoy_results", "balancesheet", "cashflow", "ratios", "shareholding_pattern_quarterly", "profit_loss_stats"))
    for k, labels in (("sales", ("Sales", "Revenue")), ("op", ("Operating Profit", "Financing Profit")), ("oi", ("Other Income",)), ("intr", ("Interest",)),
                      ("dep", ("Depreciation",)), ("pbt", ("Profit before tax",)), ("np", ("Net Profit",)), ("eps", ("EPS in Rs",))):
        o[k + "_ttm"] = _ttm(y, *labels)
        v, pv, _ = _last(y, *labels)
        o[k + "_fy"], o[k + "_g_fy"] = v, _growth(v, pv)
    o["opm_ttm"] = _ttm(y, "OPM %", "Financing Margin %")
    o["tax_pct"] = _last(y, "Tax %")[0]
    o["payout"] = _last(y, "Dividend Payout %")[0]
    for k, labels in (("eq_cap", ("Equity Capital",)), ("reserves", ("Reserves",)), ("borrow", ("Borrowings", "Borrowing")), ("oth_liab", ("Other Liabilities",)),
                      ("tot_liab", ("Total Liabilities",)), ("fixed", ("Fixed Assets",)), ("cwip", ("CWIP",)), ("invest", ("Investments",)),
                      ("oth_assets", ("Other Assets",)), ("tot_assets", ("Total Assets",))):
        o[k] = _last(bs, *labels)[0]
    if o["eq_cap"] is not None and o["reserves"] is not None:
        o["networth"] = o["eq_cap"] + o["reserves"]
        o["de"] = o["borrow"] / o["networth"] if o["borrow"] is not None and o["networth"] > 0 else None
    for k, lb in (("cfo", "Cash from Operating Activity"), ("cfi", "Cash from Investing Activity"), ("cff", "Cash from Financing Activity"),
                  ("ncf", "Net Cash Flow"), ("fcf", "Free Cash Flow"), ("cfo_op", "CFO/OP")):
        o[k] = _last(cf, lb)[0]
    for k, lb in (("debtor_days", "Debtor Days"), ("inv_days", "Inventory Days"), ("pay_days", "Days Payable"), ("ccc", "Cash Conversion Cycle"),
                  ("wc_days", "Working Capital Days"), ("roce", "ROCE %")):
        o[k] = _last(rt, lb)[0]
    if o.get("op_ttm") is not None and o.get("intr_ttm"):
        o["int_cover"] = o["op_ttm"] / o["intr_ttm"] if o["intr_ttm"] > 0 else None
    for k, lb in (("promoters", "Promoters"), ("fiis", "FIIs"), ("diis", "DIIs"), ("govt", "Government"), ("public", "Public"), ("holders", "No. of Shareholders")):
        v, pv, _ = _last(sh, lb)
        o[k] = v
        if k in ("promoters", "fiis", "diis") and v is not None and pv is not None:
            o[k + "_chg"] = v - pv
    for k, lb in (("sales_cagr", "Compounded Sales Growth"), ("profit_cagr", "Compounded Profit Growth"), ("price_cagr", "Stock Price CAGR"), ("roe_avg", "Return on Equity")):
        for per, v in (ps.get(lb) or {}).items():
            tag = "10y" if per.startswith("10") else "5y" if per.startswith("5") else "3y" if per.startswith("3") else "1y" if per.startswith("1 Year") else "ttm" if per.startswith("TTM") else "last" if per.startswith("Last") else None
            if tag:
                o[f"{k}_{tag}"] = _pct(v)
    return o


def _load():
    con = sqlite3.connect(f"file:{IA}?mode=ro", uri=True, timeout=30)
    per = {}
    for sym, stat, body in con.execute("SELECT sym, stat, body FROM ia_stat"):
        try:
            per.setdefault(sym, {})[stat] = json.loads(body)
        except (TypeError, ValueError):
            pass
    con.close()
    rows = {}
    for sym, st in per.items():
        try:
            rows[sym] = _one(st)
        except Exception:
            continue
    return pd.DataFrame.from_dict(rows, orient="index")


def table(snap):
    """One row per stock in the snapshot: statement columns, valuation from today's price, market-cap class and listing board."""
    try:
        key = (IA.stat().st_mtime, len(snap), str(snap["asof"].iloc[0]))
    except OSError:
        key = (None, len(snap), str(snap["asof"].iloc[0]))
    if _cache["key"] == key:
        return _cache["df"]
    try:
        df = _load().reindex(snap.index)
    except sqlite3.Error:
        df = pd.DataFrame(index=snap.index)
    last, mcap = snap["last"], snap["mcap"] if "mcap" in snap else pd.Series(np.nan, index=snap.index)
    num = lambda c: df[c].astype(float) if c in df else pd.Series(np.nan, index=snap.index)
    pos = lambda s: s.where(s > 0)
    df["pe"] = last / pos(num("eps_ttm"))
    df["pb"] = mcap / pos(num("networth"))
    df["ps"] = mcap / pos(num("sales_ttm"))
    df["pfcf"] = mcap / pos(num("fcf"))
    df["earn_yield"] = num("np_ttm") / mcap * 100
    df["fcf_yield"] = num("fcf") / mcap * 100
    df["ev_ebitda"] = (mcap + num("borrow").fillna(0)) / pos(num("op_ttm"))
    df["roe"] = num("np_fy") / pos(num("networth")) * 100
    # market-cap class by rank (the AMFI convention: top 100 large, next 150 mid, the rest small), and micro under Rs 500 Cr
    rk = mcap.rank(ascending=False, method="first")
    df["cap_type"] = np.select([rk <= 100, rk <= 250, mcap >= 500], ["Large cap", "Mid cap", "Small cap"], "Micro cap")
    df.loc[mcap.isna(), "cap_type"] = None
    try:
        ser = pd.Series({r[0]: r[1] for r in market.connect().execute("SELECT sym, series FROM universe")})
    except Exception:
        ser = pd.Series(dtype=object)
    board = ser.reindex(snap.index).map({"SM": "NSE SME", "ST": "NSE SME", "IV": "InvIT / REIT", "RR": "InvIT / REIT"}).fillna("NSE main board")
    df["board"] = board
    # banks, lenders and insurers: operating profit, working-capital days, borrowings and free cash flow do not mean what they mean for a company
    fin = (snap["macro"] == "Financial Services") if "macro" in snap else pd.Series(False, index=snap.index)
    df["is_fin"] = fin.values
    for c in ("op_ttm", "op_fy", "op_g_fy", "opm_ttm", "ev_ebitda", "pfcf", "fcf", "fcf_yield", "cfo_op", "int_cover", "de", "debtor_days", "inv_days", "pay_days", "ccc", "wc_days", "borrow"):
        if c in df:
            df.loc[fin.values, c] = np.nan
    df = df.replace([np.inf, -np.inf], np.nan)
    _cache.update(key=key, df=df)
    return df
