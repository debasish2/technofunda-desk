"""Quarters newer than NSE's XBRL feed, read from BSE result PDFs (rupees crore).

The PDFs carry an OCR text layer: digits are split ("1 ,910", "5 5"), so values are
rebuilt from word positions. Results are right-aligned in columns, and every row of a
column is grouped by its right edge. One filing yields three quarters (current,
preceding, year-ago), so a handful of PDFs per company cover a year or more.

Nothing is guessed: each parsed quarter is arithmetic-checked, and anything that fails
is stored with a flag instead of being silently trusted.
"""
import io
import re
from datetime import date, datetime, timedelta

import pdfplumber
import requests

HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com/",
        "Origin": "https://www.bseindia.com", "Accept": "application/json"}
ANN = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={page}&strCat=Result"
       "&strPrevDate={frm}&strScrip={scrip}&strSearch=P&strToDate={to}&strType=C&subcategory=-1")
PDF = "https://www.bseindia.com/xml-data/corpfiling/{where}/{name}"
NUMTOK = re.compile(r"^[\(\)\d,.\-–]+$")
MONTHS = {m: i for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                                      "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}


# ---------------------------------------------------------------- listing / download
def result_announcements(scrip, since, until=None, session=None):
    """BSE result announcements with an attachment, newest first (windows under 12 months)."""
    s = session or requests.Session()
    hi = until or date.today()
    out = []
    while hi > since:
        lo = max(since, hi - timedelta(days=360))
        page = 1
        while True:
            r = s.get(ANN.format(page=page, frm=lo.strftime("%Y%m%d"), to=hi.strftime("%Y%m%d"), scrip=scrip),
                      headers=HEAD, timeout=30).json()
            rows = r.get("Table") or []
            out += [x for x in rows if x.get("ATTACHMENTNAME")]
            if len(rows) < 50:
                break
            page += 1
        hi = lo - timedelta(days=1)
    return sorted(out, key=lambda x: x["DT_TM"], reverse=True)


def fetch_pdf(name, session=None):
    s = session or requests.Session()
    for where in ("AttachLive", "AttachHis"):
        r = s.get(PDF.format(where=where, name=name), headers=HEAD, timeout=120)
        if r.ok and r.content[:4] == b"%PDF":
            return r.content
    return None


# ---------------------------------------------------------------- PDF parsing
def _num(tokens):
    """['1', ',910'] -> 1910.0 ; ['(', '41)'] -> -41.0 ; ['-'] -> 0.0"""
    t = "".join(tokens).replace(",", "").replace("–", "-").strip()
    if t in ("-", ""):
        return 0.0
    neg = "(" in t or t.startswith("-")
    t = re.sub(r"[()\-]", "", t)
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _lines(page, words=None, tol=2.5):
    rows = []
    for w in sorted(words if words is not None else page.extract_words(x_tolerance=1.5),
                    key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0] - w["top"]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return [sorted(r[1], key=lambda w: w["x0"]) for r in rows]


_OCR = None


def ocr_words(page, dpi=200):
    """Render the page and OCR it; returns words in PDF points, same shape as pdfplumber's."""
    global _OCR
    if _OCR is None:
        from rapidocr import RapidOCR
        _OCR = RapidOCR()
    import numpy as np
    img = np.array(page.to_image(resolution=dpi).original.convert("RGB"))
    res = _OCR(img)
    k = 72.0 / dpi
    out = []
    for box, txt in zip(res.boxes if res.boxes is not None else [], res.txts or []):
        xs, ys = [pt[0] for pt in box], [pt[1] for pt in box]
        x0, x1 = min(xs) * k, max(xs) * k
        top = (min(ys) + max(ys)) / 2 * k                  # centre line, steadier than the top edge
        parts = txt.split()
        total = sum(len(t) for t in parts) or 1
        cur = x0
        for t in parts:                                     # spread a multi-token box over its width
            w = (x1 - x0) * len(t) / total
            out.append({"text": t, "x0": cur, "x1": cur + w, "top": top})
            cur += w
    return out


UNIT_RE = re.compile(r"(?:\bin\s+|[(\u20b9]\s*|\brs\.?\s*)(?:rs\.?\s*|inr\s*|\u20b9\s*)?(millions?|mn|lakhs?|lacs?|crores?|cr)\b", re.I)
UNIT_DIV = {"million": 10.0, "millions": 10.0, "mn": 10.0, "lakh": 100.0, "lakhs": 100.0, "lac": 100.0, "lacs": 100.0,
            "crore": 1.0, "crores": 1.0, "cr": 1.0}


def _unit_divisor(*texts):
    """Divisor to convert the page's unit to crore; (divisor, found). Reads the stated unit, never guesses."""
    for t in texts:
        m = UNIT_RE.search(t or "")
        if m:
            return UNIT_DIV[m.group(1).lower()], True
    return 1.0, False


def _label(words, left=1e9):
    """Row label: words left of the numbers, lower-cased, leading enumerators ("a)", "V", "3") removed."""
    label = " ".join(w["text"] for w in words if w["x0"] < left).strip().lower()
    for _ in range(2):
        label = re.sub(r"^(?:\(?[a-z]\)|\(?[ivx]+\)?|\d+[.)]?|[a-z][.)])\s+(?=\S)", "", label)
    return label


ROWS = {
    "sales": [r"^total revenue from operations", r"^revenue from operations", r"^(?:net )?sales|^income from operations"],
    "other": [r"^other income"],
    "tinc": [r"^total income"],
    "texp": [r"^total expenses", r"^total expenditure"],
    "assoc": [r"^share of (?:net )?profit.*(?:associates|joint ventures)"],
    "fin": [r"^finance cost", r"^interest (?:and finance )?(?:cost|expense)"],
    "dep": [r"^depreciation"],
    "pbe": [r"^profit.*before exceptional.*tax", r"^profit.*before tax and exceptional"],
    "exc": [r"^exceptional item"],
    "pbt": [r"^profit.*before tax"],
    "tax": [r"^total tax", r"^tax expense", r"^income tax expense\s*\(a"],
    "np": [r"^net profit.*for the (?:period|quarter)", r"^profit.*after tax",
           r"^profit.*for the (?:period|quarter|year)"],
    "owners": [r"^owners of the (?:company|parent)|^equity holders"],
    "eps": [r"^basic"],
}


def parse_page(page, text, words=None):
    """-> {'basis','qend','found','n','div'} for a results-table page, else None."""
    lines = _lines(page, words, tol=2.5 if words is None else 4.0)
    if words is not None:                      # the text layer's title may be garbled; use what OCR read
        head = " ".join(w["text"] for l in lines[:25] for w in l).upper()
    else:
        head = text[:1200].upper()
    if not re.search(r"FINANC\w*\W+RES\w*", head) or not re.search(r"QUART|THREE MONTHS|3 MONTHS|MONTHS ENDED", head):
        return None
    m = re.search(r"ENDED\s+(?:ON\s+)?(?:\d{1,2}(?:ST|ND|RD|TH)?\s+)?([A-Z]+)\s*(?:[\dIlO]{1,2},?\s*)?(\d{4})", head)
    if m and m.group(1) in MONTHS:
        mon, yr = MONTHS[m.group(1)], int(m.group(2))
    else:
        # no "quarter ended <date>" title: take the first column's date from the header rows, e.g.
        # "June 30, March 31, ..." followed by a row of years "2026 2026 ..."
        hdr = " ".join(w["text"] for l in lines[:40] for w in l).upper()
        md = re.search(r"\b(" + "|".join(MONTHS) + r")\s+\d{1,2},?", hdr)
        my = re.search(r"\b(20\d\d)\b", hdr[md.end():]) if md else None
        if not md or not my:
            return None
        mon, yr = MONTHS[md.group(1)], int(my.group(1))
    qend = (date(yr + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)).isoformat()

    def numeric(l):
        return [w for w in l if NUMTOK.match(w["text"]) and w["x0"] > 150]

    anchor = None
    for pat in ROWS["sales"]:
        anchor = next((l for l in lines
                       if re.match(pat, _label([w for w in l if not NUMTOK.match(w["text"])])) and len(numeric(l)) >= 2),
                      None)
        if anchor:
            break
    if anchor is None:
        return None
    nums = [w for w in anchor if NUMTOK.match(w["text"]) and w["x0"] > 150]
    if not nums:
        return None
    groups, cur = [], [nums[0]]
    for w in nums[1:]:
        if w["x0"] - cur[-1]["x1"] < 12:
            cur.append(w)
        else:
            groups.append(cur)
            cur = [w]
    groups.append(cur)
    edges = [max(w["x1"] for w in g) for g in groups]
    left = min(w["x0"] for w in groups[0]) - 12

    def cols_of(l):
        vals = [[] for _ in edges]
        for w in l:
            if w["x0"] < left or not NUMTOK.match(w["text"]):
                continue
            # nearest column edge: closing brackets hang a couple of points past it, and the
            # first digit of a long number sits well left of it, but columns are ~45pt apart
            i = min(range(len(edges)), key=lambda k: abs(edges[k] - w["x1"]))
            if -45 <= w["x1"] - edges[i] <= 8:
                vals[i].append(w["text"])
        return [(_num(v) if v else None) for v in vals]

    labelled = [(_label(l, left), l) for l in lines]
    found = {}
    for key, pats in ROWS.items():          # earlier patterns win over earlier lines
        for pat in pats:
            hit = next((cols_of(l) for lab, l in labelled
                        if re.search(pat, lab) and any(x is not None for x in cols_of(l))), None)
            if hit:
                found[key] = hit
                break
    div, known = _unit_divisor(text[:2500], head)
    consolidated = "CONSOLIDATED" in head and not re.search(r"STANDALONE", head[:head.find("CONSOLIDATED")])
    return {"basis": "Consolidated" if consolidated else "Standalone", "qend": qend,
            "found": found, "n": len(edges), "div": div, "unit_known": known}


def _shift(qend, months):
    """Quarter-end date `months` earlier (negative = later); month-end in, month-end out."""
    d = datetime.fromisoformat(qend)
    idx = d.year * 12 + (d.month - 1) - months          # month index, 0-based
    y, m = divmod(idx, 12)
    m += 1
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()


def _build(best):
    f, div, out = best["found"], best["div"], []
    for i, back in enumerate((0, 3, 12)):
        if best["n"] <= i:
            break

        def g(k):
            return f[k][i] / div if k in f and f[k][i] is not None else None

        sales, fin, dep, tinc, texp = g("sales"), g("fin"), g("dep"), g("tinc"), g("texp")
        # everything in total income beyond revenue from operations (other income, forex gains...)
        other = tinc - sales if None not in (tinc, sales) else (g("other") or 0.0)
        pbt, exc, tax, np_, own = g("pbt"), g("exc") or 0.0, g("tax"), g("np"), g("owners")
        pbe = g("pbe")
        if pbe is None and pbt is not None:
            pbe = pbt - exc
        flags = []
        if None in (sales, fin, dep, pbe):
            flags.append("missing rows")
            op = None
        else:
            op = round(pbe + fin + dep - other, 1)
        if None not in (tinc, texp, pbe):
            # profit before exceptionals may include the share of associates' profit, and some layouts
            # leave interest + depreciation out of "total expenditure"; accept either
            gap = tinc - texp + (g("assoc") or 0.0) - pbe
            if abs(gap) > 2 and abs(gap - (fin or 0) - (dep or 0)) > 2:
                flags.append("expenses do not reconcile")
        if None not in (pbt, tax, np_) and abs(pbt - tax - np_) > 2:
            flags.append("profit does not reconcile")
        npv = own if own not in (None, 0.0) else np_
        sl = [f["sales"][j] for j in range(min(3, best["n"])) if f["sales"][j]]
        if len(sl) >= 2 and max(sl) > 30 * min(sl):
            flags.append("implausible sales spread (units?)")
        if not best.get("unit_known"):
            flags.append("units not stated")
        out.append({"qend": _shift(best["qend"], back),
                    "sales": None if sales is None else round(sales, 1), "op": op,
                    "np": None if npv is None else round(npv, 1),
                    "eps": f["eps"][i] if "eps" in f else None,
                    "basis": best["basis"], "flags": flags, "ocr": bool(best.get("ocr"))})
    return out


def _check_date(qs, filed):
    """The newest quarter in a filing ends 1-92 days before it was filed (SEBI allows 45 days, 60 for the March quarter); anything else is a misread date."""
    if qs and filed:
        gap = (datetime.fromisoformat(filed[:10]) - datetime.fromisoformat(qs[0]["qend"])).days
        if not 1 <= gap <= 92:
            for q in qs:
                q["flags"].append("quarter date does not fit filing date")
    return qs


def _nflags(qs):
    return sum(len(q["flags"]) for q in qs) + 10 * sum(q["sales"] is None for q in qs) if qs else 10**6


def quarters_from_pdf(pdf_bytes, use_ocr=True, filed=None, basis=None):
    """-> list of quarter dicts (current, preceding, year-ago), each with a `flags` list.

    The PDF's own text layer is tried first. If it gives nothing or fails the arithmetic
    checks, the same results page is rendered and OCR'd, and the cleaner result is kept.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        cands = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            head = text[:1200].lower()
            if re.search(r"financ\w*\W+res\w*", head) and re.search(r"quart|three months|3 months|months ended", head):
                cands.append((page, text))
        cands.sort(key=lambda c: "consolidated" not in c[1][:1200].lower())   # consolidated first

        best = None
        for page, text in cands:
            r = parse_page(page, text)
            if r and basis and r["basis"] != basis:
                continue
            if r and {"sales", "np"} <= set(r["found"]):
                if best is None or (r["basis"] == "Consolidated" and best["basis"] != "Consolidated"):
                    best = r
                if r["basis"] == "Consolidated":
                    break
        out = _check_date(_build(best), filed) if best else []
        if use_ocr and (not out or any(q["flags"] for q in out)):
            for page, text in cands[:4]:            # OCR is ~18s a page; the results table is among the first few
                try:
                    r = parse_page(page, text, words=ocr_words(page))
                except Exception:
                    continue
                if r and basis and r["basis"] != basis:
                    continue
                if r and {"sales", "np"} <= set(r["found"]):
                    r["ocr"] = True
                    alt = _check_date(_build(r), filed)
                    if _nflags(alt) < _nflags(out):
                        out = alt
                    if not any(q["flags"] for q in out):
                        break
        return out


def _fetch_url(url, session):
    r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
    return r.content if r.ok and r.content[:4] == b"%PDF" else None


def _read(pdf, filed, basis):
    """The newer reader (backend/sources/filing.py) first: it checked out against the companies' own filings on 98% of sales figures.
    Its text-layer pass is quick; only when it finds no clean quarter does the older reader try, which can fall back to OCR."""
    try:
        from . import filing
        qs = filing.quarters_from_pdf(pdf, use_ocr=False, filed=filed, basis=basis)
    except Exception:
        qs = []
    for q in qs:                    # a unit slip shows as profit or operating profit bigger than sales, which no real quarter has
        s = abs(q["sales"] or 0)
        if s and (abs(q["np"] or 0) > 1.2 * s or abs(q["op"] or 0) > 1.5 * s or (q["op"] or 0) > 0.8 * s):
            q["flags"].append("profit or margin out of proportion to sales (units?)")
    if any(not q["flags"] for q in qs):
        return qs
    return quarters_from_pdf(pdf, filed=filed, basis=basis)


def fetch_missing(scrip, latest_have, nse_quarters=None, today=None, session=None, log=print, sym=None,
                  basis=None, last_sales=None):
    """Quarters after `latest_have` (ISO date) from results PDFs, newest filing first.

    Candidates come from BSE's announcements and, when `sym` is given, NSE's too (each exchange
    is missing some files the other has). Stops once every quarter-end between latest_have and
    today is covered by a clean parse. `nse_quarters` ({qend: row}) cross-checks overlapping columns.
    """
    from . import nse
    today = today or date.today()
    s = session or requests.Session()
    want, q = set(), latest_have
    while True:
        q = _shift(q, -3)
        if q > today.isoformat():
            break
        want.add(q)
    since = datetime.fromisoformat(latest_have).date()

    cands = [{"date": a["DT_TM"][:10], "bse": a["ATTACHMENTNAME"]} for a in result_announcements(scrip, since, today, s)]
    if sym:
        try:
            cands += [{"date": a["date"], "nse": a["url"]} for a in nse.result_announcements(sym, since, today)]
        except Exception as e:                  # NSE feed is a bonus source; BSE alone still works
            log(f"   NSE announcements unavailable: {type(e).__name__}")
    cands.sort(key=lambda c: c["date"], reverse=True)

    got, prio, done, empty = {}, {}, [], 0
    for c in cands:
        if want <= set(got) and not any(got[k]["flags"] for k in want):
            break
        d = datetime.fromisoformat(c["date"])
        if any(abs((d - x).days) <= 3 for x in done):      # same filing, listed by both exchanges
            continue
        pdf = fetch_pdf(c["bse"], s) if "bse" in c else _fetch_url(c["nse"], s)
        if not pdf and "bse" in c:
            continue
        qs = _read(pdf, c["date"], basis) if pdf else []
        if not qs:
            empty += 1
            if empty >= 4 and not got:         # layout we cannot read: stop burning time on it
                log(f"   no readable results table in {empty} filings; giving up")
                break
            continue
        done.append(d)
        ref = PDF.format(where="AttachHis", name=c["bse"]) if "bse" in c else c["nse"]
        for rank, q in enumerate(qs):
            if last_sales and q["sales"] and not 1 / 25 <= q["sales"] / last_sales <= 25:
                q["flags"].append("sales far from last filed quarter (units?)")
            if q["qend"] <= latest_have:
                if nse_quarters and q["qend"] in nse_quarters:
                    old = nse_quarters[q["qend"]]
                    if old["sales"] and q["sales"] and abs(q["sales"] - old["sales"]) > 0.02 * abs(old["sales"]):
                        log(f"   note: {q['qend']} sales {q['sales']} in the newer filing vs {old['sales']} originally filed (restated or OCR?)")
                continue
            if q["qend"] not in got or rank < prio[q["qend"]] or (not q["flags"] and got[q["qend"]]["flags"]):
                q.update(source="BSE PDF (OCR)" if q.get("ocr") else "BSE PDF", filed=c["date"], ref=ref)
                if "nse" in c:
                    q["source"] = q["source"].replace("BSE", "NSE")
                got[q["qend"]], prio[q["qend"]] = q, rank
    return [got[k] for k in sorted(got)]
