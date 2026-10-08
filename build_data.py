#!/usr/bin/env python3
"""Build data.js for Setup Desk (setup-desk.html).

Setup
    pip install yfinance pandas

Run
    python build_data.py symbols.txt                # writes data.js next to setup-desk.html
    python build_data.py symbols.txt --years 4 --out data.js

Then open setup-desk.html in a browser. It loads data.js automatically and
falls back to its sample data if the file is missing.

symbols.txt
    One NSE symbol per line, optionally followed by a sector: "STLTECH,Telecom - Equipment"
    Blank lines and lines starting with # are ignored. Peers are grouped by sector,
    so give every stock a sector if you want the peer-rank bars to mean something.

Quarterly results
    The dashboard grades each quarter against the same quarter a year earlier, so it needs
    at least 7 quarters (12 is better). yfinance usually returns only about 5, so for most
    stocks you should supply results/SYMBOL.csv yourself, in rupees crore:

        end,sales,op,np
        2023-09-30,412.5,61.2,28.4
        2023-12-31,430.1,66.0,31.9
        ...

    end   = quarter-end date (YYYY-MM-DD)
    sales = revenue from operations
    op    = operating profit (EBITDA)
    np    = net profit

    A CSV, when present, is used instead of yfinance.

Not tested against the live Yahoo feed yet. Run it on a few symbols first and
check the numbers against the company's published results.
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

CR = 1e7          # one crore
MIN_BARS = 260    # about a year of daily bars, needed for the 200-day average
MIN_Q = 7         # quarters needed for year-on-year grades and momentum
KEEP_Q = 12


def read_symbols(path):
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        out.append((parts[0].upper(), parts[1] if len(parts) > 1 else None))
    return out


def get_bars(tk, years):
    df = tk.history(period=f"{years}y", interval="1d", auto_adjust=True).dropna(subset=["Close"])
    return [
        {
            "d": idx.strftime("%Y-%m-%d"),
            "o": round(float(r.Open), 2),
            "h": round(float(r.High), 2),
            "l": round(float(r.Low), 2),
            "c": round(float(r.Close), 2),
            "v": int(r.Volume),
        }
        for idx, r in df.iterrows()
    ]


def quarters_from_csv(sym):
    p = Path("results") / f"{sym}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["end"]).sort_values("end")
    return [
        {"end": r.end.strftime("%Y-%m-%d"), "sales": float(r.sales), "op": float(r.op), "np": float(r.np)}
        for r in df.itertuples()
    ]


def _row(df, names):
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def quarters_from_yf(tk):
    df = tk.quarterly_income_stmt
    if df is None or df.empty:
        return []
    sales = _row(df, ["Total Revenue", "Operating Revenue"])
    op = _row(df, ["EBITDA", "Normalized EBITDA", "Operating Income"])
    net = _row(df, ["Net Income", "Net Income Common Stockholders"])
    if sales is None or op is None or net is None:
        return []
    out = []
    for col in sorted(df.columns):
        vals = [sales[col], op[col], net[col]]
        if any(pd.isna(v) for v in vals):
            continue
        out.append({
            "end": pd.Timestamp(col).strftime("%Y-%m-%d"),
            "sales": round(float(vals[0]) / CR, 1),
            "op": round(float(vals[1]) / CR, 1),
            "np": round(float(vals[2]) / CR, 1),
        })
    return out


def balance(tk):
    """Capital employed and equity in rupees crore, or (None, None)."""
    try:
        bs = tk.balance_sheet
        if bs is None or bs.empty:
            return None, None
        col = bs.columns[0]

        def g(*names):
            for n in names:
                if n in bs.index and not pd.isna(bs.loc[n, col]):
                    return float(bs.loc[n, col]) / CR
            return None

        ta, cl = g("Total Assets"), g("Current Liabilities")
        eq = g("Stockholders Equity", "Common Stock Equity")
        return (ta - cl if ta is not None and cl is not None else None), eq
    except Exception:
        return None, None


def build(sym, sector, years):
    tk = yf.Ticker(sym + ".NS")
    bars = get_bars(tk, years)
    if len(bars) < MIN_BARS:
        return None, f"only {len(bars)} daily bars (need {MIN_BARS})"

    quarters = quarters_from_csv(sym) or quarters_from_yf(tk)
    if len(quarters) < MIN_Q:
        return None, f"only {len(quarters)} quarters; add results/{sym}.csv (see the top of this file)"
    quarters = quarters[-KEEP_Q:]

    try:
        info = tk.info or {}
    except Exception:
        info = {}
    shares = (info.get("sharesOutstanding") or 0) / CR
    if shares <= 0:
        return None, "no share count from Yahoo"

    cap, eq = balance(tk)
    ttm_sales = sum(q["sales"] for q in quarters[-4:])
    estimated = []
    if cap is None:
        cap = round(ttm_sales / 1.2, 1)
        estimated.append("capital employed")
    if eq is None:
        eq = round(cap * 0.6, 1)
        estimated.append("equity")

    stock = {
        "sym": sym,
        "name": info.get("longName") or info.get("shortName") or sym,
        "sector": sector or info.get("industry") or info.get("sector") or "Unclassified",
        "shares": round(shares, 2),
        "capEmployed": round(cap, 1),
        "equity": round(eq, 1),
        "bars": bars,
        "quarters": quarters,
    }
    note = ("estimated " + " and ".join(estimated)) if estimated else ""
    return stock, note


def main():
    ap = argparse.ArgumentParser(description="Build data.js for Setup Desk")
    ap.add_argument("symbols", help="text file with one NSE symbol per line")
    ap.add_argument("--out", default="data.js")
    ap.add_argument("--years", type=int, default=4)
    args = ap.parse_args()

    stocks, skipped = [], []
    for sym, sector in read_symbols(args.symbols):
        try:
            stock, note = build(sym, sector, args.years)
        except Exception as e:  # network errors, delisted tickers, odd Yahoo payloads
            stock, note = None, f"error: {e}"
        if stock is None:
            skipped.append((sym, note))
            print(f"skip  {sym:<12} {note}")
        else:
            stocks.append(stock)
            print(f"ok    {sym:<12} {len(stock['bars'])} bars, {len(stock['quarters'])} quarters" + (f"  ({note})" if note else ""))

    if not stocks:
        sys.exit("No stocks built; nothing written.")
    Path(args.out).write_text("window.SETUP_DESK_DATA = " + json.dumps(stocks, separators=(",", ":")) + ";\n")
    print(f"\nWrote {args.out}: {len(stocks)} stocks, {len(skipped)} skipped.")


if __name__ == "__main__":
    main()
