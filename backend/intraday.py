"""Intraday bars for the Desk's 5m / 15m / 1h chart, from Yahoo Finance (free, about 15 minutes behind NSE).

    python -m backend.intraday TCS 5m       # print what the endpoint will return

Yahoo carries 5-minute bars for the last 60 days, 15-minute for 60 days and hourly for two years, but only for ordinary NSE equities:
SME-platform stocks and InvITs return nothing, and the Desk says so. Bars come back with their time already shifted to Indian clock time
(the chart library draws times as UTC), and a small cache keeps a minute-old answer so a busy chart does not hammer Yahoo.
"""
import sys
import time
import warnings

import yfinance as yf

warnings.filterwarnings("ignore")
IST_SHIFT = 19800                                       # seconds: UTC+5:30
SPAN = {"5m": "5d", "15m": "1mo", "1h": "6mo"}          # how far back each interval is asked for
TTL = 60
_cache = {}


def bars(sym, iv):
    if iv not in SPAN:
        raise ValueError("interval must be 5m, 15m or 1h")
    key = (sym, iv)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    try:
        h = yf.Ticker(sym + ".NS").history(period=SPAN[iv], interval=iv, auto_adjust=True, prepost=False)
    except KeyError:                                    # what Yahoo's library raises for symbols it has no intraday feed for
        h, why = None, None
    except Exception as e:
        h = None
        why = f"Yahoo Finance did not answer ({type(e).__name__})."
    else:
        why = None
    out = []
    if h is not None and len(h):
        h = h.dropna(subset=["Close"])
        for ts, r in h.iterrows():
            if min(r.Open, r.High, r.Low, r.Close) <= 0:
                continue
            out.append({"t": int(ts.timestamp()) + IST_SHIFT, "o": round(float(r.Open), 2), "h": round(float(r.High), 2),
                        "l": round(float(r.Low), 2), "c": round(float(r.Close), 2), "v": int(r.Volume or 0)})
    res = {"iv": iv, "bars": out}
    if not out:
        res["why"] = why or "No intraday prices for this stock. Yahoo Finance carries them only for ordinary NSE equities, not for SME-platform stocks or InvITs."
    else:
        day = lambda b: b["t"] // 86400
        last = day(out[-1])
        prev = [b for b in out if day(b) < last]
        if prev:
            res["prev_close"] = prev[-1]["c"]               # yesterday's close, for the dotted reference line
    _cache[key] = (time.time(), res)
    return res


def session_bar(sym):
    """The newest session as one daily candle, built from today's 5-minute bars: {d, o, h, l, c, v, prev_close, asof}, or {why} when Yahoo has none."""
    from datetime import datetime, timezone
    r = bars(sym, "5m")
    B = r["bars"]
    if not B:
        return {"why": r.get("why")}
    day = lambda b: b["t"] // 86400
    S = [b for b in B if day(b) == day(B[-1])]
    return {"d": datetime.fromtimestamp(S[0]["t"], timezone.utc).date().isoformat(),          # times were shifted to Indian clock time, so the UTC date is the Indian date
            "o": S[0]["o"], "h": max(b["h"] for b in S), "l": min(b["l"] for b in S), "c": S[-1]["c"], "v": sum(b["v"] for b in S),
            "prev_close": r.get("prev_close"), "last_bar": datetime.fromtimestamp(S[-1]["t"], timezone.utc).strftime("%H:%M")}


if __name__ == "__main__":
    r = bars(sys.argv[1].upper() if len(sys.argv) > 1 else "TCS", sys.argv[2] if len(sys.argv) > 2 else "5m")
    print({k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
    for b in r["bars"][-3:]:
        print(b)
