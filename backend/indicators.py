"""Technical indicators, relative strength and market breadth for the whole NSE universe.

Everything is computed from the daily-bar matrices in market.db (dates x symbols), so each stock's RS
rating is a percentile against all other stocks, and the breadth series come from the same data.

Definitions the screener exposes (see FIELDS at the bottom). Where the source UI named a filter without
defining it ("Momentum score", "Emerging leader"...), the definition below is mine and is spelled out
in the field's description so it can be changed deliberately.
"""
import numpy as np
import pandas as pd

from . import market

SESSIONS = 83            # length of the breadth charts


# ---------------------------------------------------------------- data
def load(con=None):
    con = con or market.connect()
    df = pd.read_sql("SELECT sym,d,o,h,l,c,v FROM mbar", con)
    wide = {k: df.pivot(index="d", columns="sym", values=k).sort_index() for k in ("o", "h", "l", "c", "v")}
    idx = pd.read_sql("SELECT d,c FROM idx", con).set_index("d")["c"]
    idx = idx.reindex(wide["c"].index).ffill()
    names = dict(con.execute("SELECT sym,name FROM universe").fetchall())
    return wide, idx, names


# ---------------------------------------------------------------- building blocks
def ema(x, n):
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(x, n):
    return x.rolling(n, min_periods=n).mean()


def pct_rank(x):
    """Row-wise percentile 1..99 across symbols (NaN stays NaN)."""
    r = x.rank(axis=1, method="min")
    n = x.notna().sum(axis=1).replace(0, np.nan)
    return (((r - 1).div(n - 1, axis=0)) * 100).clip(1, 99).round()


def supertrend(H, L, C, period=10, mult=3.0):
    """+1 = price above the trailing band (uptrend), -1 = below."""
    pc = C.shift(1)
    tr = np.maximum(H - L, np.maximum((H - pc).abs(), (L - pc).abs()))
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    hl2 = (H + L) / 2
    up, lo = (hl2 + mult * atr).values, (hl2 - mult * atr).values
    c = C.values
    T, N = c.shape
    fu, fl = up.copy(), lo.copy()
    d = np.ones((T, N))
    for t in range(1, T):
        pu, pl, pcl = fu[t - 1], fl[t - 1], c[t - 1]
        fu[t] = np.where((up[t] < pu) | (pcl > pu) | np.isnan(pu), up[t], pu)
        fl[t] = np.where((lo[t] > pl) | (pcl < pl) | np.isnan(pl), lo[t], pl)
        d[t] = np.where(c[t] > pu, 1, np.where(c[t] < pl, -1, d[t - 1]))
    out = pd.DataFrame(d, index=C.index, columns=C.columns)
    return out.where(atr.notna())


def parabolic_sar(H, L, C, step=0.02, top=0.2):
    """+1 = SAR below price (uptrend), -1 = SAR above price."""
    h, l, c = H.values, L.values, C.values
    T, N = c.shape
    trend = np.full(N, np.nan)
    sar = np.full(N, np.nan)
    ep = np.full(N, np.nan)
    af = np.full(N, step)
    out = np.full((T, N), np.nan)
    for t in range(1, T):
        valid = ~np.isnan(c[t]) & ~np.isnan(c[t - 1])
        fresh = valid & np.isnan(trend)
        trend = np.where(fresh, 1.0, trend)
        sar = np.where(fresh, np.fmin(l[t - 1], l[t]), sar)
        ep = np.where(fresh, np.fmax(h[t - 1], h[t]), ep)
        af = np.where(fresh, step, af)
        run = valid & ~fresh & ~np.isnan(trend)
        prev2_l = l[t - 2] if t >= 2 else l[t - 1]
        prev2_h = h[t - 2] if t >= 2 else h[t - 1]
        ns = sar + af * (ep - sar)
        up = trend == 1
        ns_up = np.fmin(ns, np.fmin(l[t - 1], prev2_l))
        ns_dn = np.fmax(ns, np.fmax(h[t - 1], prev2_h))
        flip_up = run & up & (l[t] < ns_up)          # uptrend broken
        flip_dn = run & ~up & (h[t] > ns_dn)         # downtrend broken
        stay_up = run & up & ~flip_up
        stay_dn = run & ~up & ~flip_dn
        new_sar = np.where(flip_up | flip_dn, ep, np.where(stay_up, ns_up, np.where(stay_dn, ns_dn, sar)))
        new_ep = np.where(flip_up, l[t], np.where(flip_dn, h[t], np.where(stay_up & (h[t] > ep), h[t],
                          np.where(stay_dn & (l[t] < ep), l[t], ep))))
        bumped = (stay_up & (h[t] > ep)) | (stay_dn & (l[t] < ep))
        af = np.where(flip_up | flip_dn, step, np.where(bumped, np.minimum(af + step, top), af))
        trend = np.where(flip_up, -1.0, np.where(flip_dn, 1.0, trend))
        sar, ep = new_sar, new_ep
        out[t] = np.where(~np.isnan(trend), trend, np.nan)
    return pd.DataFrame(out, index=C.index, columns=C.columns)


# ---------------------------------------------------------------- the screener table
# Consolidation: a stretch of bars whose highest high is within 15% of its lowest low, at least 20 sessions long.
# This is the same rule the Desk's "Patterns" button draws on the chart (setup-desk.html, consolidations()).
CONSOL_THR, CONSOL_MIN, CONSOL_MAX = 0.15, 20, 250


def consolidation_box(h, l, end, thr=CONSOL_THR, min_bars=CONSOL_MIN, max_bars=CONSOL_MAX):
    """h, l: arrays (sessions x stocks). For each stock, the longest stretch ending at row `end` whose
    high-to-low range stays within `thr`. Returns (length, top, bottom); length 0 when shorter than min_bars."""
    n = h.shape[1]
    hi, lo = np.full(n, -np.inf), np.full(n, np.inf)
    length, top, bot = np.zeros(n, int), np.full(n, np.nan), np.full(n, np.nan)
    alive = np.ones(n, bool)
    with np.errstate(invalid="ignore", divide="ignore"):
        for k in range(1, min(max_bars, end + 1) + 1):
            hi, lo = np.maximum(hi, h[end - k + 1]), np.minimum(lo, l[end - k + 1])
            alive &= (hi / lo - 1) <= thr                    # NaN prices compare False, so a gap ends the stretch
            if not alive.any():
                break
            length[alive], top[alive], bot[alive] = k, hi[alive], lo[alive]
    ok = length >= min_bars
    return np.where(ok, length, 0), np.where(ok, top, np.nan), np.where(ok, bot, np.nan)


# VCP (volatility contraction pattern): in an uptrend, a base made of pullbacks that each get shallower, resting just under a pivot.
# The Desk's VCP overlay uses exactly this rule (setup-desk.html, vcpScan()); keep the two in step.
#   trend    close > 150-day average > 200-day average, and the 200-day average is rising over the last month
#   base     starts at the highest high of the last 130 sessions (ignoring the last 3), at least 15 sessions ago
#   swings   a zig-zag with a 4.5% reversal: peak, trough, peak, trough ...
#   VCP      the latest run of pullbacks whose depths shrink one after the other, at least two of them
#   pivot    the high the last pullback started from
#   status   breakout: close above the pivot, up to 5% over; forming: within 8% under it and above the last low; both need the last
#            pullback to be 12% deep or less
VCP_LOOK, VCP_SWING, VCP_MIN_BASE = 130, 0.045, 15


def vcp_scan(H, L, C, V, look=VCP_LOOK, swing=VCP_SWING, min_base=VCP_MIN_BASE):
    n = len(C)
    if n < 230:
        return None
    s150, s200, s200p = C[-150:].mean(), C[-200:].mean(), C[-222:-22].mean()
    if not (C[-1] > s150 > s200 > s200p):
        return None
    start = n - look
    ib = start + int(np.argmax(H[start:n - 3]))
    if n - 1 - ib < min_base:
        return None
    piv = [("H", ib, H[ib])]
    d, lo_i, lo_v, hi_i, hi_v = -1, ib, L[ib], ib, H[ib]
    for i in range(ib + 1, n):
        if d == -1:
            if L[i] < lo_v:
                lo_i, lo_v = i, L[i]
            elif H[i] >= lo_v * (1 + swing):
                piv.append(("L", lo_i, lo_v))
                d, hi_i, hi_v = 1, i, H[i]
        else:
            if H[i] > hi_v:
                hi_i, hi_v = i, H[i]
            elif L[i] <= hi_v * (1 - swing):
                piv.append(("H", hi_i, hi_v))
                d, lo_i, lo_v = -1, i, L[i]
    if d == -1 and (piv[-1][2] - lo_v) / piv[-1][2] >= swing:          # a pullback still going on
        piv.append(("L", lo_i, lo_v))
    pairs = [(piv[k], piv[k + 1]) for k in range(0, len(piv) - 1, 2)]
    if len(pairs) < 2:
        return None
    depths = [(a[2] - b[2]) / a[2] * 100 for a, b in pairs]
    j = len(depths) - 1
    while j > 0 and depths[j - 1] > depths[j]:
        j -= 1
    run, dep = pairs[j:], depths[j:]
    if len(run) < 2:
        return None
    pivot, last_low = run[-1][0][2], run[-1][1][2]
    price = C[-1]
    tight = dep[-1] <= 12                                  # the last pullback must be tight to count as a setup
    if price > pivot:
        status = ("breakout" if tight else "loose") if price / pivot - 1 <= 0.05 else "extended"
    elif (pivot / price - 1) <= 0.08 and price >= last_low and tight:
        status = "forming"
    else:
        status = "loose"
    dry = bool(V[run[-1][0][1]:].mean() < 0.9 * V[ib:].mean())
    return {"status": status, "n": len(run), "depths": dep, "pivot": float(pivot), "dist": float((pivot / price - 1) * 100),
            "dry": dry, "base": ib, "pts": [(q[1], float(q[2]), q[0]) for a, b in run for q in (a, b)]}


def snapshot(data=None):
    """One row per stock for the newest session, plus the RS history needed by leader filters."""
    wide, idx, names = data or load()
    C, H, L, V = wide["c"], wide["h"], wide["l"], wide["v"]
    e20, e50, e200 = ema(C, 20), ema(C, 50), ema(C, 200)
    s50, s150, s200 = sma(C, 50), sma(C, 150), sma(C, 200)
    ret = {k: C / C.shift(k) - 1 for k in (21, 63, 126, 189, 252)}
    rs_raw = 0.4 * ret[63] + 0.2 * ret[126] + 0.2 * ret[189] + 0.2 * ret[252]
    rs = pct_rank(rs_raw)                                  # IBD-style weighted rating, 1-99
    rs1m, rs3m, rs6m, rs12m = (pct_rank(ret[k]) for k in (21, 63, 126, 252))
    hi52, lo52 = H.rolling(252, min_periods=200).max(), L.rolling(252, min_periods=200).min()

    line = C.div(idx, axis=0)                              # RS line = stock / Nifty 500
    mansfield = (line / sma(line, 252) - 1) * 100
    line_hi = line.rolling(252, min_periods=200).max()
    mom = pct_rank(0.4 * ret[63] + 0.3 * ret[126] + 0.3 * ret[21])      # recent-weighted momentum

    st, sar = supertrend(H, L, C), parabolic_sar(H, L, C)
    t = len(C) - 1
    last = C.iloc[t]
    row = lambda m: m.iloc[t]
    rising200 = s200.iloc[t] > s200.iloc[t - 22]
    template = pd.DataFrame({
        "t1": (last > row(s150)) & (last > row(s200)),
        "t2": row(s150) > row(s200),
        "t3": rising200,
        "t4": (last > row(s50)) & (row(s50) > row(s150)),
        "t5": (last >= 1.3 * row(lo52)) & (last >= 0.75 * row(hi52)),
    })
    stage = pd.Series(1, index=C.columns)
    stage[(last < row(s150)) & (row(s200) >= s200.iloc[t - 22])] = 3
    stage[(last < row(s150)) & (row(s150) < row(s200)) & (row(s200) < s200.iloc[t - 22])] = 4
    stage[(last > row(s150)) & (row(s150) > row(s200)) & rising200] = 2
    rs_now, rs_63 = row(rs), rs.iloc[t - 63]
    hv, lv, cv = H.values, L.values, C.values
    box_n, box_top, box_bot = consolidation_box(hv, lv, t)
    broke = np.zeros(len(C.columns), bool)                 # closed above the top of a box that ended 1-5 sessions ago
    for j in range(1, 6):
        n_j, top_j, _ = consolidation_box(hv, lv, t - j)
        with np.errstate(invalid="ignore"):
            broke |= (n_j > 0) & (cv[t] > top_j)
    vc = {}
    Hm, Lm, Vm = H.values, L.values, V.values
    for ci, sym in enumerate(C.columns):
        h, l, c, v = Hm[-260:, ci], Lm[-260:, ci], cv[-260:, ci], Vm[-260:, ci]
        if np.isnan(c).any() or np.isnan(h).any() or np.isnan(l).any():
            continue
        r = vcp_scan(h, l, c, np.nan_to_num(v))
        if r:
            vc[sym] = r
    snap = pd.DataFrame({
        "name": pd.Series(names), "last": last, "chg": (C.iloc[t] / C.iloc[t - 1] - 1) * 100,
        "value_cr": (C * V).rolling(20, min_periods=10).mean().iloc[t] / 1e7,       # avg daily traded value
        "ema20": row(e20), "ema50": row(e50), "ema200": row(e200),
        "sma50": row(s50), "sma150": row(s150), "sma200": row(s200),
        "stage": stage.where(row(s200).notna()),
        "template": template.sum(axis=1).where(row(s200).notna()),
        "supertrend": row(st), "sar": row(sar),
        "rs": rs_now, "rs1m": row(rs1m), "rs3m": row(rs3m), "rs6m": row(rs6m), "rs12m": row(rs12m),
        "vs500_55": ((C.iloc[t] / C.iloc[t - 55] - 1) - (idx.iloc[t] / idx.iloc[t - 55] - 1)) * 100,
        "vs500_123": ((C.iloc[t] / C.iloc[t - 123] - 1) - (idx.iloc[t] / idx.iloc[t - 123] - 1)) * 100,
        "mansfield": row(mansfield),
        "rs_high": row(line) >= 0.999 * row(line_hi),
        "emerging": (rs_now >= 70) & (rs_63 < 70) & (last > row(e50)),
        "fading": (rs_63 >= 80) & (rs_now <= rs_63 - 15),
        "momentum": row(mom),
        "high52_pct": (last / row(hi52) - 1) * 100,
        "consol_bars": pd.Series(box_n, index=C.columns), "consol_active": pd.Series(box_n > 0, index=C.columns),
        "consol_range": pd.Series((box_top / box_bot - 1) * 100, index=C.columns),
        "consol_breakout": pd.Series(broke, index=C.columns),
        "vcp_status": pd.Series({k: v["status"] for k, v in vc.items()}, dtype=object).reindex(C.columns),
        "vcp_n": pd.Series({k: v["n"] for k, v in vc.items()}, dtype=float).reindex(C.columns),
        "vcp_last": pd.Series({k: v["depths"][-1] for k, v in vc.items()}, dtype=float).reindex(C.columns),
        "vcp_dist": pd.Series({k: v["dist"] for k, v in vc.items()}, dtype=float).reindex(C.columns),
        "vcp_dry": pd.Series({k: v["dry"] for k, v in vc.items()}, dtype=object).reindex(C.columns).fillna(False).astype(bool),
    })
    snap.index.name = "sym"
    snap["asof"] = C.index[t]
    return snap.dropna(subset=["last"])


# ---------------------------------------------------------------- breadth
def breadth(data=None, sessions=SESSIONS):
    wide, idx, names = data or load()
    C, H, L = wide["c"], wide["h"], wide["l"]
    chg = C.pct_change(fill_method=None)
    e50, e200 = ema(C, 50), ema(C, 200)
    traded = C.notna() & C.shift(1).notna()
    adv = ((chg > 0) & traded).sum(axis=1)
    dec = ((chg < 0) & traded).sum(axis=1)
    hi52 = H.rolling(252, min_periods=200).max()
    lo52 = L.rolling(252, min_periods=200).min()
    n50 = e50.notna() & C.notna()
    n200 = e200.notna() & C.notna()
    out = pd.DataFrame({
        "advancing": adv, "declining": dec,
        "up4": ((chg >= 0.04) & traded).sum(axis=1), "down4": ((chg <= -0.04) & traded).sum(axis=1),
        "traded": traded.sum(axis=1),
        "above50": ((C > e50) & n50).sum(axis=1), "above200": ((C > e200) & n200).sum(axis=1),
        "base50": n50.sum(axis=1), "base200": n200.sum(axis=1),
        "new_high": ((C >= 0.98 * hi52) & hi52.notna()).sum(axis=1),
        "new_low": ((C <= 1.02 * lo52) & lo52.notna()).sum(axis=1),
    }).tail(sessions)
    out["ad_line"] = (out["advancing"] - out["declining"]).cumsum()
    out["pct50"] = out["above50"] / out["base50"] * 100
    out["pct200"] = out["above200"] / out["base200"] * 100
    return out


def with_live(data, rows, idx_close, d):
    """The stored history plus one provisional row (today's running bar) from backend.live. rows: (sym, o, h, l, c, v)."""
    wide, idx, names = data
    if d <= str(wide["c"].index[-1]):
        return data
    live = pd.DataFrame(rows, columns=["sym", "o", "h", "l", "c", "v"]).set_index("sym")
    out = {}
    for k in ("o", "h", "l", "c", "v"):
        add = live[k].reindex(wide[k].columns).to_frame(name=d).T
        out[k] = pd.concat([wide[k], add])
    idx2 = pd.concat([idx, pd.Series([idx_close], index=[d])])
    return out, idx2, names


def ma_pct(C, n, kind="ema"):
    """(% of stocks above their n-day average, how many stocks counted), per session. A stock counts once it has n bars and closes at Rs 1+."""
    ma = sma(C, n) if kind == "sma" else ema(C, n)
    ok = ma.notna() & C.notna() & (C >= 1)
    return ((C > ma) & ok).sum(axis=1) / ok.sum(axis=1).replace(0, np.nan) * 100, ok.sum(axis=1)


def ma_breadth(data=None, kind="ema", sessions=260, periods=(10, 20, 50, 200), cache=None):
    """Share of NSE stocks (closing at Rs 1 or more) trading above their 10, 20, 50 and 200-day moving average, session by session.
    A stock counts on a day only once it has that many bars. Also returns the 50-day average of the %-above-50 line."""
    wide, idx, names = data or load()
    C = wide["c"]
    out = {}
    for n in periods:
        if cache is not None and (kind, n) in cache:
            p_, c_ = cache[(kind, n)]
        else:
            p_, c_ = ma_pct(C, n, kind)
            if cache is not None:
                cache[(kind, n)] = (p_, c_)
        out[f"p{n}"], out[f"n{n}"] = p_, c_
    df = pd.DataFrame(out)
    if "p50" in df:
        df["avg50"] = df["p50"].rolling(50, min_periods=50).mean()
    return df.tail(sessions)


def mbi(data=None, kind="sma", thr=4.0, sessions=60):
    """The market-breadth dashboard, session by session (modelled on the Stocksgeeks MBI):
       4R   stocks up >= thr% in the day over stocks down more than thr%, times 100
       20R, 50R   stocks above their 20 / 50-day average over stocks at or below it, times 100
       chg  the day-on-day % change of each of those ratios
       NH, NL   stocks making a new 52-week high / low (beyond the previous 252 sessions' extreme)
    Universe: NSE stocks closing at Rs 1 or more that traded the day before."""
    wide, idx, names = data or load()
    C, H, L = wide["c"], wide["h"], wide["l"]
    chg = C.pct_change(fill_method=None) * 100
    ok = C.notna() & C.shift(1).notna() & (C >= 1)
    up, dn = ((chg >= thr) & ok).sum(axis=1), ((chg < -thr) & ok).sum(axis=1)
    out = {"up": up, "down": dn, "r4": up / dn.replace(0, np.nan) * 100, "traded": ok.sum(axis=1)}
    for n in (10, 20, 50, 200):
        ma = sma(C, n) if kind == "sma" else ema(C, n)
        okm = ma.notna() & C.notna() & (C >= 1)
        above, tot = ((C > ma) & okm).sum(axis=1), okm.sum(axis=1)
        out[f"a{n}"] = above / tot.replace(0, np.nan) * 100
        out[f"r{n}"] = above / (tot - above).replace(0, np.nan) * 100
    hp, lp = H.shift(1).rolling(252, min_periods=200).max(), L.shift(1).rolling(252, min_periods=200).min()
    out["nh"] = ((H > hp) & hp.notna() & ok).sum(axis=1)
    out["nl"] = ((L < lp) & lp.notna() & ok).sum(axis=1)
    df = pd.DataFrame(out)
    for c in ("r4", "r20", "r50"):
        df[c + "_chg"] = (df[c] / df[c].shift(1) - 1) * 100
    df["idx_chg"] = idx.pct_change() * 100
    return df.tail(sessions)


# ---------------------------------------------------------------- screener field catalogue
FIELDS = [
    {"group": "Trend & moving averages", "id": "price_ma", "label": "Price vs moving averages", "type": "choice",
     "options": {"above_ema20": "Above EMA 20", "above_ema50": "Above EMA 50", "above_ema200": "Above EMA 200",
                 "above_sma50": "Above SMA 50", "above_sma150": "Above SMA 150", "above_sma200": "Above SMA 200",
                 "below_ema50": "Below EMA 50", "below_ema200": "Below EMA 200"}},
    {"group": "Trend & moving averages", "id": "ma_order", "label": "Moving-average order", "type": "choice",
     "options": {"ema20_50_200": "EMA 20 > EMA 50 > EMA 200", "sma50_150_200": "SMA 50 > SMA 150 > SMA 200",
                 "ema20_50": "EMA 20 > EMA 50", "ema50_200": "EMA 50 > EMA 200"}},
    {"group": "Trend & moving averages", "id": "stage", "label": "Weinstein stage", "type": "choice",
     "options": {"1": "Stage 1 - basing", "2": "Stage 2 - advancing", "3": "Stage 3 - topping", "4": "Stage 4 - declining"}},
    {"group": "Trend & moving averages", "id": "template", "label": "Minervini trend template: at least", "type": "count", "of": 5,
     "help": "Five tests: price above SMA150 and SMA200; SMA150 above SMA200; SMA200 rising for a month; "
             "price above SMA50 and SMA50 above SMA150; price 30%+ off its 52-week low and within 25% of its high."},
    {"group": "Trend & moving averages", "id": "supertrend", "label": "Supertrend direction (10, 3)", "type": "direction"},
    {"group": "Trend & moving averages", "id": "sar", "label": "Parabolic SAR direction (0.02 / 0.2)", "type": "direction"},
    {"group": "Relative strength", "id": "rs", "label": "RS rating", "type": "range", "min": 1, "max": 99,
     "help": "Percentile (1-99) of 40% 3-month + 20% each 6, 9 and 12-month return, against every stock in the market."},
    {"group": "Relative strength", "id": "rs1m", "label": "RS 1M", "type": "range", "min": 1, "max": 99},
    {"group": "Relative strength", "id": "rs3m", "label": "RS 3M", "type": "range", "min": 1, "max": 99},
    {"group": "Relative strength", "id": "rs6m", "label": "RS 6M", "type": "range", "min": 1, "max": 99},
    {"group": "Relative strength", "id": "rs12m", "label": "RS 12M", "type": "range", "min": 1, "max": 99},
    {"group": "Relative strength", "id": "vs500_55", "label": "vs Nifty 500, 55 days >", "type": "min", "unit": "%",
     "help": "Stock's 55-session return minus the Nifty 500's, in percentage points."},
    {"group": "Relative strength", "id": "vs500_123", "label": "vs Nifty 500, 123 days >", "type": "min", "unit": "%"},
    {"group": "Relative strength", "id": "mansfield", "label": "Mansfield RS >", "type": "min", "unit": "%",
     "help": "(stock / Nifty 500) against its own 252-session average, in percent."},
    {"group": "Relative strength", "id": "rs_high", "label": "RS line at a new high", "type": "flag",
     "help": "Stock / Nifty 500 within 0.1% of its 52-week high."},
    {"group": "Relative strength", "id": "emerging", "label": "Emerging leader", "type": "flag",
     "help": "RS rating now 70+, was under 70 three months ago, and price above EMA 50."},
    {"group": "Relative strength", "id": "fading", "label": "Fading leader", "type": "flag",
     "help": "RS rating was 80+ three months ago and has fallen 15 or more points since."},
    {"group": "Relative strength", "id": "momentum", "label": "Momentum score >=", "type": "min",
     "help": "Percentile (1-99) of 40% 3-month + 30% 6-month + 30% 1-month return."},
    {"group": "Price patterns", "id": "consol_active", "label": "In a consolidation now", "type": "flag",
     "help": "A stretch of at least 20 sessions ending today whose highest high is within 15% of its lowest low "
             "(the same rule as the Desk's Patterns button). Looks back up to 250 sessions."},
    {"group": "Price patterns", "id": "consol_bars", "label": "Consolidating for at least", "type": "min", "unit": "sessions",
     "help": "How long the current consolidation has run. 20 sessions is about 4 weeks."},
    {"group": "Price patterns", "id": "consol_range", "label": "Box range at most", "type": "max", "unit": "%",
     "help": "Highest high over lowest low inside the current consolidation, in percent. Lower means a tighter base."},
    {"group": "Price patterns", "id": "vcp", "label": "Volatility contraction (VCP)", "type": "choice",
     "options": {"any": "Forming or breaking out", "forming": "Forming: within 8% under the pivot", "breakout": "Breaking out: up to 5% over the pivot"},
     "help": "In an uptrend (close above the 150-day average, itself above a rising 200-day), a base of two or more pullbacks that each get "
             "shallower, with price resting just under the pivot (the high the last pullback started from). The same rule the chart's VCP overlay uses."},
    {"group": "Price patterns", "id": "vcp_n", "label": "VCP contractions at least", "type": "min", "unit": "pullbacks"},
    {"group": "Price patterns", "id": "vcp_last", "label": "VCP last pullback at most", "type": "max", "unit": "%",
     "help": "Depth of the most recent (tightest) pullback, high to low, in percent."},
    {"group": "Price patterns", "id": "vcp_dry", "label": "VCP with volume drying up", "type": "flag",
     "help": "Average volume since the last pullback began is under 90% of the base's average."},
    {"group": "Price patterns", "id": "consol_breakout", "label": "Broke out of a consolidation (last 5 sessions)", "type": "flag",
     "help": "Closed above the top of a consolidation that ended in the last 5 sessions."},
]


def apply(snap, filters):
    """filters: {field_id: value}. Returns the matching rows, strongest RS first."""
    m = pd.Series(True, index=snap.index)
    for fid, v in filters.items():
        if v in (None, "", False):
            continue
        if fid == "price_ma":
            kind, col = v.split("_", 1)
            m &= (snap["last"] > snap[col]) if kind == "above" else (snap["last"] < snap[col])
        elif fid == "ma_order":
            cols = {"ema20_50_200": ["ema20", "ema50", "ema200"], "sma50_150_200": ["sma50", "sma150", "sma200"],
                    "ema20_50": ["ema20", "ema50"], "ema50_200": ["ema50", "ema200"]}[v]
            for a, b in zip(cols, cols[1:]):
                m &= snap[a] > snap[b]
        elif fid == "stage":
            m &= snap["stage"] == float(v)
        elif fid == "template":
            m &= snap["template"] >= float(v)
        elif fid in ("supertrend", "sar"):
            m &= snap[fid] == (1 if v == "up" else -1)
        elif fid in ("rs", "rs1m", "rs3m", "rs6m", "rs12m"):
            lo, hi = v if isinstance(v, (list, tuple)) else (v, 99)
            m &= snap[fid].between(float(lo or 1), float(hi or 99))
        elif fid in ("vs500_55", "vs500_123", "mansfield", "momentum"):
            m &= snap[fid] > float(v)
        elif fid == "consol_bars":
            m &= snap["consol_bars"] >= max(float(v), 1)
        elif fid == "consol_range":
            m &= snap["consol_range"] <= float(v)
        elif fid == "vcp":
            m &= snap["vcp_status"].isin(("forming", "breakout") if v == "any" else (v,))
        elif fid == "vcp_n":
            m &= snap["vcp_n"] >= float(v)
        elif fid == "vcp_last":
            m &= snap["vcp_last"] <= float(v)
        elif fid == "vcp_dry":
            m &= snap["vcp_dry"] & snap["vcp_status"].isin(("forming", "breakout"))
        elif fid in ("rs_high", "emerging", "fading", "consol_active", "consol_breakout"):
            m &= snap[fid].fillna(False).astype(bool)
        elif fid in ("macro", "sector", "industry", "basic"):
            m &= snap[fid] == v
        elif fid == "ind_top3m":
            m &= snap["ind_rank_3m"] <= float(v)
        elif fid == "ind_top1m":
            m &= snap["ind_rank_1m"] <= float(v)
        elif fid in ("ind_3m", "ind_1m"):
            m &= snap[fid] > float(v)
        elif fid == "min_value_cr":
            m &= snap["value_cr"] >= float(v)
    return snap[m].sort_values("rs", ascending=False)
