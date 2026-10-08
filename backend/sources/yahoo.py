"""Prices, share count and balance sheet from Yahoo Finance."""
import pandas as pd
import yfinance as yf

CR = 1e7


def bars(sym, years=4):
    df = yf.Ticker(sym + ".NS").history(period=f"{years}y", interval="1d", auto_adjust=True).dropna(subset=["Close"])
    return [(i.strftime("%Y-%m-%d"), round(float(r.Open), 2), round(float(r.High), 2),
             round(float(r.Low), 2), round(float(r.Close), 2), int(r.Volume)) for i, r in df.iterrows()]


def profile(sym, tk=None, info=None):
    tk = tk or yf.Ticker(sym + ".NS")
    info = info if info is not None else (tk.info or {})
    cap = eq = debt = None
    try:
        bs = tk.balance_sheet
        col = bs.columns[0]
        g = lambda *ns: next((float(bs.loc[n, col]) / CR for n in ns if n in bs.index and not pd.isna(bs.loc[n, col])), None)
        ta, cl = g("Total Assets"), g("Current Liabilities")
        eq = g("Stockholders Equity", "Common Stock Equity")
        debt = g("Total Debt")
        cap = ta - cl if ta is not None and cl is not None else None
    except Exception:
        pass
    if info.get("financialCurrency") not in (None, "INR"):      # e.g. Infosys reports its balance sheet in USD
        cap = eq = debt = None
    return {"name": info.get("longName") or sym, "shares_cr": (info.get("sharesOutstanding") or 0) / CR,
            "cap_employed": cap, "equity": eq, "debt": debt, "industry": info.get("industry")}


def quarters(sym):
    """Yahoo's recent quarters, used only as a cross-check (it often skips quarters)."""
    df = yf.Ticker(sym + ".NS").quarterly_income_stmt
    out = {}
    for col in df.columns:
        g = lambda n: float(df.loc[n, col]) / CR if n in df.index and not pd.isna(df.loc[n, col]) else None
        out[pd.Timestamp(col).strftime("%Y-%m-%d")] = {"sales": g("Total Revenue"), "op": g("EBITDA"), "np": g("Net Income")}
    return out
